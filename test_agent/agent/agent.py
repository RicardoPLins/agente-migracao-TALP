"""
Optimized Test Equivalence Agent
Flow: Analyzer → Inspector → Generator → Executor → Evaluator → Router → Report
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

EQUIVALENCE_THRESHOLD = 90.0
LLM_RETRY_ATTEMPTS    = 3
MIN_BASELINE          = 3

# ── LLM ───────────────────────────────────────────────────────────────────────

llm = ChatOpenAI(
    api_key=os.getenv("PROVIDER_API_KEY"),
    base_url=os.getenv("PROVIDER_BASE_URL"),
    model="meta-llama/llama-4-scout-17b-16e-instruct",
    temperature=0.0,
    max_tokens=4096,
)

# ── Prompts ───────────────────────────────────────────────────────────────────

PROMPT_ANALYZER = """
You are an analyzer. Receive two Python modules: ORIGINAL (urllib) and MIGRATED (requests).

Produce a concise JSON test plan with:
- original_functions: list of {name, params, endpoints, returns, error_handling}
- migrated_functions: same structure
- equivalence_pairs: list of {original, migrated, behavioral_diff}
- test_scenarios: max 12 items, each with {id, category, function, description, mock_setup, expected_original, expected_migrated, priority}
- coverage_notes: one sentence

Categories: happy_path | http_error | network_error | invalid_input | headers | response_parsing

Return ONLY valid JSON, no markdown, no explanation.

ORIGINAL:
{original_code}

MIGRATED:
{migrated_code}
""".strip()

PROMPT_INSPECTOR = """
You are a code inspector. Analyze the two Python modules below and answer specific questions about their implementation.

Return ONLY valid JSON with exactly this structure — no markdown, no explanation:

{
  "original": {
    "uses_gzip": <true|false>,
    "response_strip_chars": <int — how many leading chars are stripped before JSON parse, e.g. 9 for responseData[9:], 0 if none>,
    "raises_on_http_error": <true|false>,
    "raises_on_network_error": <true|false>,
    "generateRequestData_return_type": <"bytes"|"dict"|"str"|"other">,
    "local_imports": [],
    "missing_imports": []
  },
  "migrated": {
    "uses_gzip": <true|false>,
    "response_strip_chars": <int>,
    "raises_on_http_error": <true|false>,
    "raises_on_network_error": <true|false>,
    "generateRequestData_return_type": <"bytes"|"dict"|"str"|"other">,
    "local_imports": [],
    "missing_imports": []
  },
  "behavioral_diffs": []
}

ORIGINAL CODE:
{original_code}

MIGRATED CODE:
{migrated_code}
""".strip()

PROMPT_GENERATOR = """
You are a Python test engineer. Generate a single pytest file that proves functional equivalence between original_module and migrated_module.

═══════════════════════════════════════════════════════
URLLIB → REQUESTS MIGRATION
The two libraries behave differently. Read the rules carefully.
═══════════════════════════════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #1 — NO REAL HTTP REQUESTS
Every test MUST mock ALL network calls.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #2 — urllib SPECIFICS (original_module)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A) If uses_gzip=true in MODULE QUIRKS, the original module reads the response
   through gzip.GzipFile(fileobj=response). The mock MUST serve real gzip bytes:

   PAYLOAD = {"key": "value"}
   compressed = gzip.compress(json.dumps(PAYLOAD).encode())
   buf = io.BytesIO(compressed)
   mock_resp = MagicMock()
   mock_resp.read.side_effect = buf.read   # ← side_effect = buf.read (callable)
   mock_resp.readable.return_value = True
   mock_resp.seekable.return_value = False
   mock_resp.writable.return_value = False
   mock_resp.__enter__ = lambda s: s
   mock_resp.__exit__ = MagicMock(return_value=False)

   If uses_gzip=false, use plain bytes:
   mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')

B) response_strip_chars slice (N chars) happens INSIDE the module — do NOT apply
   it in your tests. When mocking internal methods, include the prefix:

   PREFIX = "X" * N   # N = response_strip_chars from MODULE QUIRKS
   with patch.object(scraper, 'executeRequest', return_value=PREFIX + json.dumps(PAYLOAD)):
       scraper.scrapeConversation(False)

C) urllib errors:
   - Network: side_effect=urllib.error.URLError("reason")
   - HTTP:    side_effect=urllib.error.HTTPError(url, code, msg, {}, None)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #3 — requests SPECIFICS (migrated_module)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A) requests decompresses gzip automatically — no gzip in mocks.

B) If response_strip_chars > 0 in MODULE QUIRKS, the migrated executeRequest
   strips that many chars internally. The mock body MUST include the prefix:

   PREFIX = "X" * <response_strip_chars>
   responses.add(responses.POST, 'https://example.com/endpoint',
                 body=PREFIX + json.dumps(PAYLOAD), status=200)

   DO NOT use json=PAYLOAD — use body=PREFIX+json.dumps(PAYLOAD).

C) HTTP error assertions — ONLY assert raises if raises_on_http_error=true
   in MODULE QUIRKS for that module.

D) requests errors:
   - Network: responses.add(..., body=requests.exceptions.ConnectionError())
   - HTTP:    responses.add(..., status=404)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #4 — WHAT TO TEST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Good candidates:
  - generateRequestData: pure method, no mocking.
    original returns bytes (urlencode+encode), migrated returns dict.
    Normalize for comparison.
  - executeRequest: mock HTTP layer as shown above
  - __init__ attribute checks

Bad candidates (skip):
  - scrapeConversation: too many filesystem side effects
  - main(): calls sys.exit

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #5 — CONSTRUCTOR DIFFERENCES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The two classes WILL have different __init__ signatures.
Read them carefully and instantiate each correctly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #6 — MANDATORY IMPORTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import responses
import requests
from unittest.mock import MagicMock, patch
import pytest
from original_module import ConversationScraper as OriginalConversationScraper
from migrated_module import ConversationScraper as MigratedConversationScraper

CRITICAL: ALWAYS use OriginalConversationScraper and MigratedConversationScraper.
NEVER use bare ConversationScraper.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #7 — OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Output ONLY valid Python code. No markdown fences, no explanations, no TODO.

MODULE QUIRKS:
{module_quirks}

ORIGINAL CODE:
{original_code}

MIGRATED CODE:
{migrated_code}

TEST PLAN:
{test_plan}
""".strip()

PROMPT_REPORT = """
You are a report generator. Output a concise Markdown report.

Structure:
# Equivalence Test Report
**Generated:** {timestamp}

## Decision: [APPROVED / CONDITIONAL / REJECTED]
[1 sentence summary]

## Test Results
| Metric | Value |
|---|---|
| Valid baseline (tests passing on original) | N |
| Passed on both (confirmed equivalent) | N |
| Regressions (pass original, fail migrated) | N |
| Inversions (fail original, pass migrated) | N |
| Symmetric failures (fail on both — noise) | N |
| Equivalence rate (regression-based) | N% |

## Behavior Change Analysis
### Regressions (migration broke something)
[list or "None"]

### Inversions (migration fixed or changed something)
[list or "None — or note if this looks like a bug fix vs behavior change"]

### Symmetric Failures (generation noise, ignored)
[list or "None"]

## Coverage
| Module | Line Coverage |
|---|---|
| Original | N% |
| Migrated | N% |

**Note:** coverage measures code execution, not behavioral equivalence.

## Warnings
[unreliable results, generation errors, or "None"]

Rules:
- APPROVED if equivalence_rate >= 95% and valid_baseline >= 3
- CONDITIONAL if equivalence_rate >= 85% and valid_baseline >= 3
- REJECTED if equivalence_rate < 85% OR valid_baseline < 3
- Output ONLY Markdown

EVALUATION:
{final_evaluation}

TIMESTAMP: {timestamp}
""".strip()

# ── Utils ─────────────────────────────────────────────────────────────────────

def clean_llm_response(raw: str) -> str:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    fence = re.search(r"```(?:json|python|markdown)?\n?(.*?)```", raw, flags=re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return raw.strip()


def _invoke_llm(prompt: str, attempts: int = LLM_RETRY_ATTEMPTS) -> str:
    for attempt in range(1, attempts + 1):
        try:
            result = clean_llm_response(llm.invoke([HumanMessage(content=prompt)]).content)
            if result.strip():
                return result
            print(f"  [LLM] Attempt {attempt}: empty response, retrying...")
        except Exception as e:
            msg = str(e)
            if "tokens per day" in msg.lower() or "TPD" in msg.lower():
                print(f"  [LLM] Daily token limit reached — stopping retries")
                break
            print(f"  [LLM] Attempt {attempt}: error — {e}")
    return ""


def _parse_json(raw: str, fallback: dict) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _validate_test_code(code: str, module_quirks: dict | None = None) -> tuple[bool, str]:
    if not code.strip():
        return False, "Empty test code"
    if not re.search(r"def test_\w+", code):
        return False, "No test_ functions found"
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"

    has_any_mock = bool(re.search(r"MagicMock|patch\(|@patch|responses\.", code))
    if not has_any_mock:
        return False, "No mocking found — tests would make real HTTP requests"

    # Rejeitar responses como context manager
    if re.search(r"with\s+responses\.activate\s*\(\)", code):
        return False, "responses.activate() used as context manager — use @responses.activate decorator"
    if re.search(r"with\s+responses\.RequestsMock\s*\(\)", code):
        return False, "responses.RequestsMock() used as context manager — use @responses.activate decorator"

    # Checar gzip: só rejeita se não tiver nenhuma referência a gzip no código inteiro
    uses_gzip = (module_quirks or {}).get("original", {}).get("uses_gzip", False)
    if uses_gzip and not re.search(r"gzip\.", code):
        return False, (
            "urllib mock missing gzip — original uses gzip compression. "
            "Use gzip.compress() and buf.read as side_effect"
        )

    for test_name in re.findall(r"def (test_\w+)", code):
        match = re.search(
            rf"def {re.escape(test_name)}\([^)]*\):(.*?)(?=\ndef |\Z)",
            code, re.DOTALL,
        )
        if match:
            body = match.group(1)
            calls_network = bool(re.search(r"\.(executeRequest|scrapeConversation)\(", body))
            has_local_mock = bool(re.search(r"patch\(|responses\.|MagicMock", body))
            if calls_network and not has_local_mock:
                return False, f"{test_name} calls network method without mock"

    return True, "OK"


def _parse_pytest_json(report_path: Path) -> dict:
    empty = {
        "total": 0, "passed": 0, "failed": 0,
        "errors": 0, "skipped": 0,
        "failed_tests": [], "passed_tests": [],
    }
    if not report_path.exists():
        return empty
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        s = data.get("summary", {})
        tests = data.get("tests", [])
        return {
            "total":        s.get("total", 0),
            "passed":       s.get("passed", 0),
            "failed":       s.get("failed", 0),
            "errors":       s.get("errors", 0),
            "skipped":      s.get("skipped", 0),
            "failed_tests": [t["nodeid"] for t in tests if t.get("outcome") in ("failed", "error")],
            "passed_tests": [t["nodeid"] for t in tests if t.get("outcome") == "passed"],
        }
    except Exception as e:
        print(f"  [Executor] JSON report parse error: {e}")
        return empty


def _parse_coverage_json(cov_path: Path) -> float:
    if not cov_path.exists():
        return 0.0
    try:
        data = json.loads(cov_path.read_text(encoding="utf-8"))
        totals  = data.get("totals", {})
        covered = totals.get("covered_lines", 0)
        total   = totals.get("num_statements", 0)
        return round((covered / total) * 100, 2) if total > 0 else 0.0
    except Exception as e:
        print(f"  [Executor] Coverage parse error: {e}")
        return 0.0


def _sanitize_code(code: str) -> str:
    code = re.sub(
        r"^\s*from\s+util\s+import\s+logger.*$",
        "import logging; logger = logging.getLogger(__name__)",
        code, flags=re.MULTILINE,
    )
    code = re.sub(r"^\s*from\s+\.\w+\s+import\s+.*$",
                  "# relative import removed", code, flags=re.MULTILINE)
    code = re.sub(r"^\s*sys\.path\.append\(.*__file__.*\).*$",
                  "# sys.path removed", code, flags=re.MULTILINE)
    return code


def _run_pytest_debug_file(tmpdir: Path, filename: str = "test_equivalence.py") -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", filename, "-v", "--tb=long", "--no-header"],
        cwd=tmpdir, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    debug_path = Path(__file__).parent / f"debug_{filename.replace('.py', '')}.txt"
    try:
        debug_path.write_text(output, encoding="utf-8")
    except Exception:
        pass
    return output


# ── State ─────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    original_code:           str
    migrated_code:           str
    test_plan:               dict
    module_quirks:           dict
    test_code:               str
    pytest_summary:          dict
    coverage:                dict
    regressions:             list
    evaluation:              dict
    generation_error:        bool
    generation_error_reason: str
    router_decision:         dict
    report:                  str


# ── Node 1: Analyzer ──────────────────────────────────────────────────────────

def node_analyzer(state: AgentState) -> AgentState:
    print("[Analyzer] Analyzing codebases...")
    prompt = (
        PROMPT_ANALYZER
        .replace("{original_code}", state["original_code"])
        .replace("{migrated_code}", state["migrated_code"])
    )
    fallback = {"critical_paths": [], "edge_cases": [],
                "expected_behaviors": [], "test_scenarios": []}
    test_plan = _parse_json(_invoke_llm(prompt), fallback)
    print(f"[Analyzer] {len(test_plan.get('test_scenarios', []))} scenarios planned")
    return {**state, "test_plan": test_plan}


# ── Node 2: Inspector ─────────────────────────────────────────────────────────

def node_inspector(state: AgentState) -> AgentState:
    print("[Inspector] Extracting module quirks...")
    prompt = (
        PROMPT_INSPECTOR
        .replace("{original_code}", state["original_code"])
        .replace("{migrated_code}", state["migrated_code"])
    )
    fallback = {
        "original": {
            "uses_gzip": False, "response_strip_chars": 0,
            "raises_on_http_error": False, "raises_on_network_error": False,
            "generateRequestData_return_type": "unknown",
            "local_imports": [], "missing_imports": [],
        },
        "migrated": {
            "uses_gzip": False, "response_strip_chars": 0,
            "raises_on_http_error": False, "raises_on_network_error": False,
            "generateRequestData_return_type": "unknown",
            "local_imports": [], "missing_imports": [],
        },
        "behavioral_diffs": [],
    }
    quirks = _parse_json(_invoke_llm(prompt), fallback)
    for side in ("original", "migrated"):
        if side not in quirks or not isinstance(quirks[side], dict):
            quirks[side] = fallback[side]
        for key, default in fallback["original"].items():
            quirks[side].setdefault(key, default)
    quirks.setdefault("behavioral_diffs", [])
    print("[Inspector] Quirks detected:")
    for side in ("original", "migrated"):
        q = quirks[side]
        print(f"  [{side}] gzip={q['uses_gzip']} strip={q['response_strip_chars']} "
              f"http_error={q['raises_on_http_error']} "
              f"generateRequestData={q['generateRequestData_return_type']}")
    for diff in quirks["behavioral_diffs"]:
        print(f"  [diff] {diff}")
    return {**state, "module_quirks": quirks}


# ── Node 3: Generator ────────────────────────────────────────────────────────

def node_generator(state: AgentState) -> AgentState:
    quirks = state.get("module_quirks", {})

    print("[Generator] Generating unit tests...")
    prompt = (
        PROMPT_GENERATOR
        .replace("{original_code}", state["original_code"])
        .replace("{migrated_code}", state["migrated_code"])
        .replace("{test_plan}",     json.dumps(state["test_plan"], indent=2))
        .replace("{module_quirks}", json.dumps(quirks, indent=2))
    )
    test_code = ""
    error = False
    reason = ""
    last_error = ""
    
    for attempt in range(1, LLM_RETRY_ATTEMPTS + 1):
        retry_prompt = prompt if not last_error else (
            prompt + f"\n\n# PREVIOUS ATTEMPT FAILED\n# Error: {last_error}\n"
            "# Fix exactly this issue and regenerate the complete file."
        )
        raw = _invoke_llm(retry_prompt, attempts=1)
        valid, msg = _validate_test_code(raw, quirks)
        if valid:
            test_code = raw
            print(f"[Generator] Valid unit tests on attempt {attempt}")
            try:
                out_dir = Path(__file__).parent / "generated_tests"
                out_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                (out_dir / f"test_equivalence_{ts}.py").write_text(test_code, encoding="utf-8")
                print(f"[Generator] Saved to generated_tests/test_equivalence_{ts}.py")
            except Exception:
                pass
            break
        last_error = msg
        print(f"  [Generator] Attempt {attempt} invalid: {msg}")
        reason = msg
    
    if not test_code:
        error = True
        print(f"[Generator] WARNING: unit tests failed after {LLM_RETRY_ATTEMPTS} attempts — {reason}")

    return {**state, "test_code": test_code,
            "generation_error": error, "generation_error_reason": reason}


# ── Node 4: Executor ─────────────────────────────────────────────────────────

def node_executor(state: AgentState) -> AgentState:
    print("[Executor] Running tests...")
    empty_summary = {
        "total": 0, "passed": 0, "failed": 0,
        "errors": 0, "skipped": 0,
        "failed_tests": [], "passed_tests": [],
    }
    if state.get("generation_error"):
        print("[Executor] Skipping — generation error")
        return {**state,
                "pytest_summary": {"original": empty_summary, "migrated": empty_summary},
                "coverage": {"original": 0.0, "migrated": 0.0}}

    for pkg, mod in [("pytest", "pytest"),
                     ("pytest-json-report", "pytest_jsonreport"),
                     ("pytest-cov", "coverage")]:
        if importlib.util.find_spec(mod) is None:
            raise RuntimeError(f"Missing: {pkg}. Run: pip install pytest pytest-json-report pytest-cov responses")

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        d.joinpath("original_module.py").write_text(_sanitize_code(state["original_code"]), encoding="utf-8")
        d.joinpath("migrated_module.py").write_text(_sanitize_code(state["migrated_code"]), encoding="utf-8")
        d.joinpath("test_equivalence.py").write_text(state["test_code"], encoding="utf-8")

        debug_out = _run_pytest_debug_file(d, "test_equivalence.py")
        print(f"  [Executor] pytest preview:\n{debug_out[:800]}\n  ...")

        def run(module: str) -> tuple[dict, float]:
            rjson = d / f"report_{module}.json"
            cjson = d / f"cov_{module}.json"
            subprocess.run(
                [sys.executable, "-m", "pytest", "test_equivalence.py",
                 "-v", "--tb=short",
                 "--json-report", f"--json-report-file={rjson}",
                 f"--cov={module}", f"--cov-report=json:{cjson}"],
                cwd=d, capture_output=True, text=True, timeout=120,
            )
            return _parse_pytest_json(rjson), _parse_coverage_json(cjson)

        s_orig, cov_orig = run("original_module")
        s_mig,  cov_mig  = run("migrated_module")

    print(f"[Executor] Tests done — original: {s_orig['passed']}p/{s_orig['failed']}f "
          f"| migrated: {s_mig['passed']}p/{s_mig['failed']}f")

    return {
        **state,
        "pytest_summary": {"original": {**s_orig, "coverage": cov_orig},
                           "migrated":  {**s_mig,  "coverage": cov_mig}},
        "coverage": {"original": cov_orig, "migrated": cov_mig},
    }


# ── Node 5: Evaluator ────────────────────────────────────────────────────────

def node_evaluator(state: AgentState) -> AgentState:
    print("[Evaluator] Evaluating...")

    orig = state["pytest_summary"]["original"]
    mig  = state["pytest_summary"]["migrated"]

    if state.get("generation_error"):
        evaluation = {
            "execution_summary": {
                "original_passed": 0, "original_failed": 0,
                "migrated_passed": 0, "migrated_failed": 0,
                "valid_baseline": 0, "confirmed_equivalent": 0,
                "regressions": 0, "inversions": 0, "symmetric_failures": 0,
                "regression_rate": 0.0, "equivalence_rate": 0.0,
            },
            "coverage": state["coverage"],
            "coverage_note": "Coverage does not imply behavioral equivalence.",
            "regressions_detected": [], "inversions_detected": [],
            "symmetric_failures": [],
            "scores": {"overall": 0.0},
            "status": "FAIL",
            "failure_reason": state.get("generation_error_reason", "unknown"),
            "unreliable_results": True,
        }
        return {**state, "evaluation": evaluation, "regressions": []}

    orig_passed = set(orig.get("passed_tests", []))
    orig_failed = set(orig.get("failed_tests", []))
    mig_passed  = set(mig.get("passed_tests",  []))
    mig_failed  = set(mig.get("failed_tests",  []))

    regressions        = sorted(mig_failed & orig_passed)
    inversions         = sorted(mig_passed & orig_failed)
    symmetric_failures = sorted(mig_failed & orig_failed)
    confirmed          = sorted(mig_passed & orig_passed)

    valid_baseline   = orig["passed"]
    regression_count = len(regressions)
    unreliable       = valid_baseline < MIN_BASELINE

    if valid_baseline > 0:
        regression_rate = round((regression_count / valid_baseline) * 100, 2)
        equiv = round(((valid_baseline - regression_count) / valid_baseline) * 100, 2)
    else:
        regression_rate = 0.0
        equiv = 0.0

    status = "PASS" if (equiv >= EQUIVALENCE_THRESHOLD and not unreliable) else "FAIL"

    if unreliable:
        print(f"[Evaluator] WARNING: baseline={valid_baseline} < {MIN_BASELINE} — results unreliable")

    evaluation = {
        "execution_summary": {
            "original_passed":      orig["passed"],
            "original_failed":      orig["failed"],
            "migrated_passed":      mig["passed"],
            "migrated_failed":      mig["failed"],
            "valid_baseline":       valid_baseline,
            "confirmed_equivalent": len(confirmed),
            "regressions":          regression_count,
            "inversions":           len(inversions),
            "symmetric_failures":   len(symmetric_failures),
            "regression_rate":      regression_rate,
            "equivalence_rate":     equiv,
        },
        "coverage": {
            "original": state["coverage"].get("original", 0.0),
            "migrated": state["coverage"].get("migrated", 0.0),
        },
        "coverage_note": (
            "Coverage measures code execution, not behavioral equivalence. "
            "Two implementations can have identical coverage with different outputs."
        ),
        "regressions_detected":  regressions,
        "inversions_detected":   inversions,
        "symmetric_failures":    symmetric_failures,
        "confirmed_equivalent":  confirmed,
        "scores":                {"overall": equiv},
        "status":                status,
        "unreliable_results":    unreliable,
    }

    print(
        f"[Evaluator] baseline={valid_baseline} confirmed={len(confirmed)} "
        f"regressions={regression_count} inversions={len(inversions)} "
        f"noise={len(symmetric_failures)} equiv={equiv:.1f}% | status={status}"
        + (" (UNRELIABLE)" if unreliable else "")
    )

    return {**state, "evaluation": evaluation, "regressions": regressions}


# ── Node 6: Router ───────────────────────────────────────────────────────────

def node_router(state: AgentState) -> AgentState:
    print("[Router] Deciding...")

    ev = state.get("evaluation", {})
    es = ev.get("execution_summary", {})

    valid_baseline      = es.get("valid_baseline", 0)
    unit_regressions    = es.get("regressions", 0)
    unit_inversions     = es.get("inversions", 0)
    unit_equiv          = es.get("equivalence_rate", 0.0)
    unreliable          = ev.get("unreliable_results", True)
    evaluator_status    = ev.get("status", "UNKNOWN")

    needs_revision = False
    reasons        = []
    suggestions    = []

    if evaluator_status == "FAIL" and not unreliable:
        if unit_regressions > 0:
            needs_revision = True
            failed = ev.get("regressions_detected", [])
            reasons.append(f"{unit_regressions} regression(s) — migration broke working behavior")
            suggestions.append(
                f"Tests that passed on original but fail on migrated: {failed}. "
                f"Review response parsing, error handling, and return value transformations."
            )
        
        if unit_inversions > 0:
            inv = ev.get("inversions_detected", [])
            reasons.append(
                f"{unit_inversions} inversion(s) — FAILED on original but PASS on migrated: {inv}"
            )
        
        if not needs_revision:
            equiv_pct = unit_equiv
            reasons.append(
                f"Equivalence {equiv_pct}% below {EQUIVALENCE_THRESHOLD}% threshold"
            )
            needs_revision = True
            suggestions.append(
                "Multiple behaviors that worked in original are broken in migrated. "
                "Check response parsing, data transformation, and error handling."
            )
    
    elif unreliable or valid_baseline < MIN_BASELINE:
        needs_revision = False
        reasons = [f"Baseline too low ({valid_baseline} < {MIN_BASELINE}) — result is inconclusive"]
        suggestions = ["Fix test generation quality before re-evaluating the migration."]
    
    else:
        needs_revision = False
        reasons = ["All regressions below threshold — migration is equivalent"]

    decision = {
        "needs_revision":       needs_revision,
        "reasons":              reasons,
        "suggestions":          suggestions,
        "regressions":          unit_regressions,
        "inversions":           unit_inversions,
        "equivalence_rate":     unit_equiv,
        "valid_baseline":       valid_baseline,
        "evaluator_status":     evaluator_status,
        "regressions_detected": ev.get("regressions_detected", []),
        "inversions_detected":  ev.get("inversions_detected", []),
    }

    verdict = "NEEDS_REVISION" if needs_revision else "APPROVED"
    print(f"[Router] Decision: {verdict}")
    for r in reasons:
        print(f"  → {r}")

    return {**state, "router_decision": decision}


# ── Node 7: Report ───────────────────────────────────────────────────────────

def node_report(state: AgentState) -> AgentState:
    print("[Report] Generating report...")
    prompt = (
        PROMPT_REPORT
        .replace("{final_evaluation}", json.dumps(state["evaluation"], indent=2))
        .replace("{timestamp}",        datetime.now().isoformat())
    )
    report = _invoke_llm(prompt)
    if not report:
        status = state["evaluation"].get("status", "UNKNOWN")
        report = f"# Equivalence Report\n\n**Status:** {status}\n\n*Report generation failed.*"
    return {**state, "report": report}


# ── Graph ─────────────────────────────────────────────────────────────────────

def build_graph():
    from langgraph.graph import END, StateGraph
    
    g = StateGraph(AgentState)
    for name, fn in [("analyzer",  node_analyzer),
                     ("inspector", node_inspector),
                     ("generator", node_generator),
                     ("executor",  node_executor),
                     ("evaluator", node_evaluator),
                     ("router",    node_router),
                     ("report",    node_report)]:
        g.add_node(name, fn)
    g.set_entry_point("analyzer")
    g.add_edge("analyzer",  "inspector")
    g.add_edge("inspector", "generator")
    g.add_edge("generator", "executor")
    g.add_edge("executor",  "evaluator")
    g.add_edge("evaluator", "router")
    g.add_edge("router",    "report")
    g.add_edge("report",    END)
    return g.compile()


# ── Entrypoint ────────────────────────────────────────────────────────────────

def run_agent(original_code: str, migrated_code: str) -> dict:
    return build_graph().invoke({
        "original_code":           original_code,
        "migrated_code":           migrated_code,
        "test_plan":               {},
        "module_quirks":           {},
        "test_code":               "",
        "pytest_summary":          {},
        "coverage":                {},
        "regressions":             [],
        "evaluation":              {},
        "generation_error":        False,
        "generation_error_reason": "",
        "router_decision":         {},
        "report":                  "",
    })


# ── Mock Input ─────────────────────────────────────────────────────────────────

MOCK_ORIGINAL = """
import urllib.request, json

def get_user(user_id: int) -> dict:
    url = f"https://api.example.com/users/{user_id}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())
"""

MOCK_MIGRATED = """
import requests

def get_user(user_id: int) -> dict:
    r = requests.get(f"https://api.example.com/users/{user_id}", timeout=10)
    r.raise_for_status()
    return r.json()
"""

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run equivalence test agent")
    parser.add_argument("--original", help="Path to original (urllib) Python file")
    parser.add_argument("--migrated", help="Path to migrated (requests) Python file")
    parser.add_argument("--mock",     action="store_true", help="Use built-in mock input")
    parser.add_argument("--output",   default="report.md")
    args = parser.parse_args()

    if args.mock:
        original, migrated = MOCK_ORIGINAL, MOCK_MIGRATED
        print("[main] Using mock input")
    else:
        if not args.original or not args.migrated:
            parser.error("--original and --migrated are required")
        original = Path(args.original).read_text(encoding="utf-8")
        migrated = Path(args.migrated).read_text(encoding="utf-8")

    result = run_agent(original, migrated)
    Path(args.output).write_text(result["report"], encoding="utf-8")

    print(f"\n✅ Report saved to {args.output}")
    ev = result.get("evaluation", {})
    es = ev.get("execution_summary", {})
    rd = result.get("router_decision", {})

    print(f"   Status:              {ev.get('status', 'N/A')}")
    print(f"   Valid baseline:      {es.get('valid_baseline', 'N/A')} tests")
    print(f"   Confirmed equiv:     {es.get('confirmed_equivalent', 'N/A')} tests")
    print(f"   Regressions:         {es.get('regressions', 'N/A')}")
    print(f"   Inversions:          {es.get('inversions', 'N/A')}")
    print(f"   Symmetric noise:     {es.get('symmetric_failures', 'N/A')}")
    print(f"   Equivalence rate:    {es.get('equivalence_rate', 'N/A')}%")
    print(f"   Coverage (original): {ev.get('coverage', {}).get('original', 'N/A')}%")
    print(f"   Coverage (migrated): {ev.get('coverage', {}).get('migrated', 'N/A')}%")

    verdict = "NEEDS_REVISION" if rd.get("needs_revision") else "APPROVED"
    print(f"\n   Router decision:     {verdict}")
    for reason in rd.get("reasons", []):
        print(f"   → {reason}")
    for suggestion in rd.get("suggestions", []):
        print(f"   💡 {suggestion}")

    if result.get("generation_error"):
        print(f"   ⚠️  Generation error: {result.get('generation_error_reason')}")
    if ev.get("unreliable_results"):
        print("   ⚠️  Results unreliable: baseline too low")