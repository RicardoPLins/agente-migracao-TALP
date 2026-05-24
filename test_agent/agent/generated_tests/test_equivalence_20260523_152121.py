"""
Optimized Test Equivalence Agent
Flow: Analyzer → Generator → Executor → Evaluator → Report
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
from langgraph.graph import END, StateGraph

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

EQUIVALENCE_THRESHOLD = 90.0
LLM_RETRY_ATTEMPTS = 3

# ── LLM ───────────────────────────────────────────────────────────────────────

llm = ChatOpenAI(
    api_key=os.getenv("PROVIDER_API_KEY"),
    base_url=os.getenv("PROVIDER_BASE_URL"),
    model="llama-3.3-70b-versatile",
    temperature=0.0,
    max_tokens=4096,
)

# ── Prompts embutidos ─────────────────────────────────────────────────────────

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
    "raises_on_http_error": <true|false — does it raise an exception on HTTP 4xx/5xx?>,
    "raises_on_network_error": <true|false>,
    "generateRequestData_return_type": <"bytes"|"dict"|"str"|"other">,
    "local_imports": [<list of local/relative import lines that won't resolve in isolation>],
    "missing_imports": [<list of stdlib/third-party modules used but not imported at top level, e.g. "urllib.error">]
  },
  "migrated": {
    "uses_gzip": <true|false>,
    "response_strip_chars": <int>,
    "raises_on_http_error": <true|false — does it call raise_for_status() or equivalent?>,
    "raises_on_network_error": <true|false>,
    "generateRequestData_return_type": <"bytes"|"dict"|"str"|"other">,
    "local_imports": [<list of local/relative import lines>],
    "missing_imports": [<list of modules used but not imported>]
  },
  "behavioral_diffs": [
    <list of strings describing concrete behavioral differences between original and migrated>
  ]
}

ORIGINAL CODE:
{original_code}

MIGRATED CODE:
{migrated_code}
""".strip()

PROMPT_GENERATOR = """
You are a Python test engineer. Generate a single pytest file that proves functional equivalence between original_module (urllib) and migrated_module (requests).

═══════════════════════════════════════════════════════
THIS IS ALWAYS A urllib → requests MIGRATION.
The two libraries behave differently. Read the rules below carefully.
═══════════════════════════════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #1 — NO REAL HTTP REQUESTS
Every test MUST mock ALL network calls.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #2 — urllib SPECIFICS (original_module)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A) urllib responses are gzip-compressed.
   The original module does: gzip.GzipFile(fileobj=response).read()
   This means the mock object itself is used as a file-like object by GzipFile.
   You MUST wrap the compressed bytes in io.BytesIO and make the mock delegate to it:

   import gzip, io, json
   from unittest.mock import MagicMock, patch

   PAYLOAD = {"key": "value"}
   compressed = gzip.compress(json.dumps(PAYLOAD).encode())
   buf = io.BytesIO(compressed)

   mock_resp = MagicMock()
   mock_resp.read.side_effect = buf.read
   mock_resp.readable.return_value = True
   mock_resp.seekable.return_value = False
   mock_resp.writable.return_value = False
   mock_resp.__enter__ = lambda s: s
   mock_resp.__exit__ = MagicMock(return_value=False)

   with patch('urllib.request.urlopen', return_value=mock_resp):
       result = original_module.some_method(args)

   DO NOT use mock_resp.read.return_value = compressed — GzipFile needs a real
   file-like object whose .read() is called multiple times during decompression.

B) urllib responses have leading characters stripped before JSON parse.
   Look at the source code for the slice (e.g. responseData[9:]).
   The mock return value of executeRequest MUST include that many prefix chars:

   PREFIX = "X" * 9   # adjust length to match the actual slice in the source
   mock_scraper.executeRequest = MagicMock(
       return_value=PREFIX + json.dumps(PAYLOAD)
   )

C) urllib errors:
   - Network error:  side_effect=urllib.error.URLError("reason")
   - HTTP error:     side_effect=urllib.error.HTTPError(url, code, msg, {}, None)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #3 — requests SPECIFICS (migrated_module)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A) requests decompresses gzip automatically — no gzip needed in mocks.
   Use responses library:

   import responses

   @responses.activate
   def test_migrated():
       responses.add(responses.POST, 'https://example.com/endpoint',
                     json={"key": "value"}, status=200)
       result = migrated_module.some_method(args)
       assert result is not None

B) requests responses have NO leading characters to strip.
   Mock body is plain JSON — no prefix needed.

C) requests errors:
   - Network error:  side_effect=requests.exceptions.ConnectionError()
   - Timeout:        side_effect=requests.exceptions.Timeout()
   - HTTP error:     responses.add(..., status=404) then r.raise_for_status()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #4 — WHAT TO TEST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Good candidates (test these):
  - generateRequestData / similar pure methods — no mocking needed
    NOTE: original returns bytes (urlencode+encode), migrated returns dict.
    Do NOT assert original == migrated directly — assert both contain the same keys/values.
    Example: parse the original bytes with urllib.parse.parse_qs and compare to migrated dict.
  - executeRequest — mock the HTTP layer as shown above
  - __init__ attribute checks

Bad candidates (skip these):
  - scrapeConversation / main loop methods — too many filesystem side effects
  - main() — calls sys.exit

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #5 — CONSTRUCTOR DIFFERENCES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The two classes WILL have different __init__ signatures.
Read them carefully and instantiate each one with its own correct arguments.
Do NOT assume they share the same constructor.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #6 — OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Output ONLY valid Python code. No markdown fences, no explanations, no TODO comments.

MODULE QUIRKS (facts extracted directly from the source code — trust these over your assumptions):
{module_quirks}

Use the quirks to:
- Mock gzip correctly if original.uses_gzip is true
- Only assert raises on HTTP errors if raises_on_http_error is true for that module
- Compare generateRequestData outputs correctly based on their return types

MANDATORY IMPORTS — always include ALL of these at the top of the test file:
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

IMPORTANT — response_strip_chars:
The strip (e.g. responseData[9:]) happens INSIDE scrapeConversation, NOT inside executeRequest.
executeRequest returns the raw decompressed string with NO stripping applied.
Do NOT apply [9:] to the return value of executeRequest in your tests.

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

## Metrics
| Metric | Value |
|---|---|
| Equivalence rate | N% |
| Coverage (original) | N% |
| Coverage (migrated) | N% |
| Overall score | N/10 |

## Regressions
[list or "None detected"]

## Warnings
[generation errors or unreliable results if any]

Rules:
- APPROVED if equivalence_rate >= 95%
- CONDITIONAL if equivalence_rate >= 85%
- REJECTED otherwise
- Output ONLY Markdown

EVALUATION:
{final_evaluation}

TIMESTAMP: {timestamp}
""".strip()

# ── Utils ─────────────────────────────────────────────────────────────────────

def clean_llm_response(raw: str) -> str:
    """Remove <think> blocks e markdown fences."""
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
            print(f"  [LLM] Attempt {attempt}: error — {e}")
    return ""


def _parse_json(raw: str, fallback: dict) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _validate_test_code(code: str) -> tuple[bool, str]:
    if not code.strip():
        return False, "Empty test code"
    if not re.search(r"def test_\w+", code):
        return False, "No test_ functions found"
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"

    # Verifica se há mock de rede — obrigatório
    has_urllib_mock = bool(re.search(r"patch\s*\(.*urlopen", code))
    has_requests_mock = bool(re.search(r"responses\.(activate|add)|@responses", code))
    has_any_mock = bool(re.search(r"MagicMock|patch\(|@patch|responses\.", code))

    if not has_any_mock:
        return False, "No mocking found — tests would make real HTTP requests"

    # Verifica se há testes que fazem request real sem mock (padrão perigoso)
    # Detecta chamadas a executeRequest ou scrapeConversation sem patch/responses
    dangerous_calls = re.findall(r"def (test_\w+)", code)
    for test_name in dangerous_calls:
        # Extrai o corpo do teste
        match = re.search(
            rf"def {re.escape(test_name)}\([^)]*\):(.*?)(?=\ndef |\Z)",
            code,
            re.DOTALL,
        )
        if match:
            body = match.group(1)
            calls_network = bool(re.search(r"\.(executeRequest|scrapeConversation)\(", body))
            has_local_mock = bool(re.search(r"patch\(|responses\.|MagicMock", body))
            if calls_network and not has_local_mock:
                return False, f"{test_name} calls network method without mock"

    return True, "OK"


def _parse_pytest_json(report_path: Path) -> dict:
    empty = {"total": 0, "passed": 0, "failed": 0,
             "errors": 0, "skipped": 0, "failed_tests": [], "passed_tests": []}
    if not report_path.exists():
        return empty
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        s = data.get("summary", {})
        failed_tests = [
            t["nodeid"] for t in data.get("tests", [])
            if t.get("outcome") in ("failed", "error")
        ]
        passed_tests = [
            t["nodeid"] for t in data.get("tests", [])
            if t.get("outcome") == "passed"
        ]
        return {
            "total":        s.get("total", 0),
            "passed":       s.get("passed", 0),
            "failed":       s.get("failed", 0),
            "errors":       s.get("errors", 0),
            "skipped":      s.get("skipped", 0),
            "failed_tests": failed_tests,
            "passed_tests": passed_tests,
        }
    except Exception as e:
        print(f"  [Executor] JSON report parse error: {e}")
        return empty


def _parse_coverage_json(cov_path: Path) -> float:
    if not cov_path.exists():
        return 0.0
    try:
        data = json.loads(cov_path.read_text(encoding="utf-8"))
        totals = data.get("totals", {})
        covered = totals.get("covered_lines", 0)
        total   = totals.get("num_statements", 0)
        return round((covered / total) * 100, 2) if total > 0 else 0.0
    except Exception as e:
        print(f"  [Executor] Coverage parse error: {e}")
        return 0.0



def _sanitize_code(code: str) -> str:
    """
    Replace imports that won't resolve in the isolated tempdir.
    Handles local imports like 'from util import logger' and relative imports.
    """
    # from util import logger  →  standard logging
    code = re.sub(
        r"^\s*from\s+util\s+import\s+logger.*$",
        "import logging; logger = logging.getLogger(__name__)",
        code,
        flags=re.MULTILINE,
    )
    # relative imports that break in tempdir
    code = re.sub(
        r"^\s*from\s+\.\w+\s+import\s+.*$",
        "# relative import removed for isolation",
        code,
        flags=re.MULTILINE,
    )
    # sys.path.append(...__file__...) lines
    code = re.sub(
        r"^\s*sys\.path\.append\(.*__file__.*\).*$",
        "# sys.path.append removed for isolation",
        code,
        flags=re.MULTILINE,
    )
    return code

def _run_pytest_debug(tmpdir: Path) -> str:
    """Roda pytest com output completo para debug — salva em debug_pytest.txt."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "test_equivalence.py", "-v", "--tb=long", "--no-header"],
        cwd=tmpdir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    debug_path = Path(__file__).parent / "debug_pytest.txt"
    try:
        debug_path.write_text(output, encoding="utf-8")
        print(f"  [Executor] Debug output saved to {debug_path}")
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
            "uses_gzip": False,
            "response_strip_chars": 0,
            "raises_on_http_error": False,
            "raises_on_network_error": False,
            "generateRequestData_return_type": "unknown",
            "local_imports": [],
            "missing_imports": [],
        },
        "migrated": {
            "uses_gzip": False,
            "response_strip_chars": 0,
            "raises_on_http_error": False,
            "raises_on_network_error": False,
            "generateRequestData_return_type": "unknown",
            "local_imports": [],
            "missing_imports": [],
        },
        "behavioral_diffs": [],
    }

    raw = _invoke_llm(llm, prompt)
    quirks = _parse_json(raw, fallback)

    # Ensure structure is complete
    for side in ("original", "migrated"):
        if side not in quirks or not isinstance(quirks[side], dict):
            quirks[side] = fallback[side]
        for key, default in fallback["original"].items():
            quirks[side].setdefault(key, default)

    if "behavioral_diffs" not in quirks:
        quirks["behavioral_diffs"] = []

    print(f"[Inspector] Quirks detected:")
    for side in ("original", "migrated"):
        q = quirks[side]
        print(f"  [{side}] gzip={q['uses_gzip']} strip={q['response_strip_chars']} "
              f"http_error={q['raises_on_http_error']} "
              f"generateRequestData={q['generateRequestData_return_type']}")
    for diff in quirks.get("behavioral_diffs", []):
        print(f"  [diff] {diff}")

    return {**state, "module_quirks": quirks}


# ── Node 3: Generator ────────────────────────────────────────────────────────

def node_generator(state: AgentState) -> AgentState:
    print("[Generator] Generating tests...")

    prompt = (
        PROMPT_GENERATOR
        .replace("{original_code}", state["original_code"])
        .replace("{migrated_code}", state["migrated_code"])
        .replace("{test_plan}",     json.dumps(state["test_plan"], indent=2))
        .replace("{module_quirks}", json.dumps(state.get("module_quirks", {}), indent=2))
    )

    test_code = ""
    error = False
    reason = ""

    for attempt in range(1, LLM_RETRY_ATTEMPTS + 1):
        raw = _invoke_llm(prompt, attempts=1)
        valid, msg = _validate_test_code(raw)
        if valid:
            test_code = raw
            print(f"[Generator] Valid tests on attempt {attempt}")
            try:
                out_dir = Path(__file__).parent / "generated_tests"
                out_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                (out_dir / f"test_equivalence_{ts}.py").write_text(test_code, encoding="utf-8")
                print(f"[Generator] Saved to generated_tests/test_equivalence_{ts}.py")
            except Exception:
                pass
            break
        print(f"  [Generator] Attempt {attempt} invalid: {msg}")
        reason = msg

    if not test_code:
        error = True
        print(f"[Generator] WARNING: failed after {LLM_RETRY_ATTEMPTS} attempts — {reason}")

    return {**state, "test_code": test_code,
            "generation_error": error, "generation_error_reason": reason}


# ── Node 3: Executor ─────────────────────────────────────────────────────────

def node_executor(state: AgentState) -> AgentState:
    print("[Executor] Running tests...")

    empty_summary = {"total": 0, "passed": 0, "failed": 0,
                     "errors": 0, "skipped": 0, "failed_tests": []}

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
        d.joinpath("test_equivalence.py").write_text(state["test_code"],    encoding="utf-8")

        # Roda debug primeiro para capturar erros visíveis
        debug_out = _run_pytest_debug(d)
        # Mostra primeiros 800 chars no terminal para diagnóstico rápido
        preview = debug_out[:800].replace("\r\n", "\n")
        print(f"  [Executor] pytest preview:\n{preview}\n  ...")

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

    print(f"[Executor] Done — original: {s_orig['passed']}p/{s_orig['failed']}f | migrated: {s_mig['passed']}p/{s_mig['failed']}f")
    return {
        **state,
        "pytest_summary": {"original": {**s_orig, "coverage": cov_orig},
                           "migrated":  {**s_mig,  "coverage": cov_mig}},
        "coverage": {"original": cov_orig, "migrated": cov_mig},
    }


# ── Node 4: Evaluator ────────────────────────────────────────────────────────

def node_evaluator(state: AgentState) -> AgentState:
    print("[Evaluator] Evaluating...")

    orig = state["pytest_summary"]["original"]
    mig  = state["pytest_summary"]["migrated"]

    if state.get("generation_error"):
        evaluation = {
            "execution_summary": {
                "original_passed":  0,
                "original_failed":  0,
                "migrated_passed":  0,
                "migrated_failed":  0,
                "valid_baseline":   0,
                "regressions":      0,
                "regression_rate":  0.0,
            },
            "coverage": state["coverage"],
            "regressions_detected": [],
            "scores": {"overall": 0.0},
            "status": "FAIL",
            "failure_reason": state.get("generation_error_reason", "unknown"),
        }
        return {**state, "evaluation": evaluation, "regressions": []}

    # Tests that passed on original = valid baseline for comparison
    # Tests that passed on original but failed on migrated = real regressions
    # Tests that failed on both = noise (bad mock/generation issue), ignored
    orig_passed_set = set(orig.get("passed_tests", []))
    mig_failed_set  = set(mig["failed_tests"])
    orig_failed_set = set(orig["failed_tests"])

    # Regressions: passed on original, failed on migrated
    regressions = [t for t in mig_failed_set if t not in orig_failed_set]

    # Symmetric failures: failed on both — generation noise, not regressions
    symmetric_failures = [t for t in mig_failed_set if t in orig_failed_set]

    valid_baseline = orig["passed"]  # tests that passed on original
    regression_count = len(regressions)

    # Regression rate: of tests that passed on original, how many regressed?
    if valid_baseline > 0:
        regression_rate = round((regression_count / valid_baseline) * 100, 2)
        equiv = round(((valid_baseline - regression_count) / valid_baseline) * 100, 2)
    else:
        regression_rate = 0.0
        equiv = 0.0

    unreliable = valid_baseline == 0

    if unreliable:
        print("[Evaluator] WARNING: 0 tests passed on original — no valid baseline for comparison")

    status = "PASS" if (equiv >= EQUIVALENCE_THRESHOLD and not unreliable) else "FAIL"

    evaluation = {
        "execution_summary": {
            "original_passed":  orig["passed"],
            "original_failed":  orig["failed"],
            "migrated_passed":  mig["passed"],
            "migrated_failed":  mig["failed"],
            "valid_baseline":   valid_baseline,
            "regressions":      regression_count,
            "symmetric_failures": len(symmetric_failures),
            "regression_rate":  regression_rate,
            "equivalence_rate": equiv,
        },
        "coverage": {
            "original": state["coverage"].get("original", 0.0),
            "migrated": state["coverage"].get("migrated", 0.0),
        },
        "regressions_detected":   regressions,
        "symmetric_failures":     symmetric_failures,
        "scores":                 {"overall": equiv},
        "status":                 status,
        "unreliable_results":     unreliable,
    }

    print(
        f"[Evaluator] baseline={valid_baseline} regressions={regression_count} "
        f"symmetric_noise={len(symmetric_failures)} equiv={equiv:.1f}% status={status}"
        + (" (UNRELIABLE — no baseline)" if unreliable else "")
    )

    return {**state, "evaluation": evaluation, "regressions": regressions}


# ── Node 5: Report ───────────────────────────────────────────────────────────

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
    g = StateGraph(AgentState)
    for name, fn in [("analyzer",  node_analyzer),
                     ("inspector", node_inspector),
                     ("generator", node_generator),
                     ("executor",  node_executor),
                     ("evaluator", node_evaluator),
                     ("report",    node_report)]:
        g.add_node(name, fn)
    g.set_entry_point("analyzer")
    g.add_edge("analyzer",  "inspector")
    g.add_edge("inspector", "generator")
    g.add_edge("generator", "executor")
    g.add_edge("executor",  "evaluator")
    g.add_edge("evaluator", "report")
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
        "report":                  "",
    })


# ── Mock de entrada ───────────────────────────────────────────────────────────

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
    parser.add_argument("--mock",   action="store_true", help="Use mock input")
    parser.add_argument("--output", default="report.md")
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
    print(f"   Status: {ev.get('status', 'N/A')}")
    print(f"   Equivalence: {ev.get('execution_summary', {}).get('equivalence_rate', 'N/A')}%")
    if result.get("generation_error"):
        print(f"   ⚠️  Generation error: {result.get('generation_error_reason')}")
    if ev.get("unreliable_results"):
        print("   ⚠️  Results unreliable: 0 tests passed on original module")