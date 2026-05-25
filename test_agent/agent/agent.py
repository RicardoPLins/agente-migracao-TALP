"""
Optimized Test Equivalence Agent
Flow: Analyzer → Inspector → Generator → Executor → Evaluator → Report
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

import urllib.request as _urllib_request

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI  # noqa: F401 (kept for reference)
from langchain_ollama import ChatOllama
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

EQUIVALENCE_THRESHOLD = 90.0
LLM_RETRY_ATTEMPTS = 3

# ── LLM backend detection (runs once at import time) ─────────────────────────

_OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
_OLLAMA_MODEL = os.getenv("TEST_OLLAMA_MODEL", os.getenv("REVIEW_OLLAMA_MODEL", "llama3.1:8b"))


def _detectar_ollama_test() -> tuple[bool, str]:
    """Returns (available, model_name). 2s timeout so missing Ollama never blocks startup."""
    try:
        with _urllib_request.urlopen(f"{_OLLAMA_HOST}/api/tags", timeout=2) as resp:
            data = json.loads(resp.read())
        modelos = [m["name"] for m in data.get("models", [])]
        if not modelos:
            return False, ""
        if _OLLAMA_MODEL in modelos:
            return True, _OLLAMA_MODEL
        match = next((m for m in modelos if m.startswith(_OLLAMA_MODEL.split(":")[0])), None)
        return (True, match) if match else (True, modelos[0])
    except Exception:
        return False, ""


_OLLAMA_DISPONIVEL, _OLLAMA_MODEL_ATIVO = _detectar_ollama_test()

if _OLLAMA_DISPONIVEL:
    print(f"  [test_agent] Ollama detected — model: {_OLLAMA_MODEL_ATIVO}")
else:
    print("  [test_agent] Ollama not detected — using Groq llama-3.3-70b-versatile")


def _get_llm():
    """Lazy LLM factory — avoids module-level ChatOllama/ChatGroq instantiation."""
    if _OLLAMA_DISPONIVEL:
        return ChatOllama(model=_OLLAMA_MODEL_ATIVO, base_url=_OLLAMA_HOST, temperature=0.0)
    return ChatGroq(model="llama-3.3-70b-versatile", temperature=0.0)


llm = _get_llm()

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
   The mock object itself is used as a file-like object by GzipFile.
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

B) The response_strip_chars slice (e.g. responseData[9:]) happens INSIDE
   scrapeConversation, NOT inside executeRequest.
   executeRequest returns the raw decompressed string — do NOT apply any slice
   to the return value of executeRequest in your tests.
   When mocking executeRequest for scrapeConversation tests, the mock return value
   MUST include the prefix so the internal slice produces valid JSON:

   PREFIX = "X" * 9   # must match response_strip_chars from module_quirks
   mock_scraper.executeRequest = MagicMock(
       return_value=PREFIX + json.dumps(PAYLOAD)
   )

C) urllib errors:
   - Network error: side_effect=urllib.error.URLError("reason")
   - HTTP error:    side_effect=urllib.error.HTTPError(url, code, msg, {}, None)

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
   - Network error: side_effect=requests.exceptions.ConnectionError()
   - Timeout:       side_effect=requests.exceptions.Timeout()
   - HTTP error:    responses.add(..., status=404) — only raises if raise_for_status() is called

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #4 — WHAT TO TEST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Good candidates (test these):
  - generateRequestData — pure method, no mocking needed
    NOTE: original returns bytes (urlencode+encode), migrated returns dict.
    Do NOT assert original == migrated directly.
    Parse original bytes with urllib.parse.parse_qs then compare values to migrated dict.
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
RULE #6 — MANDATORY IMPORTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Always include ALL of these at the top of the test file:

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
NEVER use bare ConversationScraper — it is not defined and will cause NameError.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #7 — OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Output ONLY valid Python code. No markdown fences, no explanations, no TODO comments.

MODULE QUIRKS (facts extracted directly from the source code — trust these over your assumptions):
{module_quirks}

Use the quirks to:
- Mock gzip correctly if original.uses_gzip is true
- Only assert raises on HTTP errors if raises_on_http_error is true for that module
- Compare generateRequestData outputs correctly based on their return types

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
| Valid baseline (tests passing on original) | N |
| Regressions detected | N |
| Symmetric failures (generation noise) | N |
| Coverage (original) | N% |
| Coverage (migrated) | N% |

## Regressions
[list test names or "None detected"]

## Symmetric Failures (ignored in scoring)
[list test names or "None"]

## Warnings
[unreliable results, generation errors, or "None"]

Rules:
- APPROVED if equivalence_rate >= 95% and valid_baseline > 0
- CONDITIONAL if equivalence_rate >= 85% and valid_baseline > 0
- REJECTED otherwise
- Output ONLY Markdown

EVALUATION:
{final_evaluation}

TIMESTAMP: {timestamp}
""".strip()


PROMPT_E2E = """
You are a Python test engineer. Generate end-to-end pytest tests that verify functional equivalence between original_module and migrated_module by testing the COMPLETE flow — not individual methods.

═══════════════════════════════════════════════════════
E2E TESTING PHILOSOPHY
Mock ONLY the network layer (HTTP calls). Let everything else run for real:
- generateRequestData runs for real
- executeRequest runs for real (except the HTTP call inside)
- scrapeConversation runs for real (filesystem, JSON parsing, etc.)
Compare the FINAL OUTPUT (files written to disk) between original and migrated.
═══════════════════════════════════════════════════════

MANDATORY RULES:

1. Use pytest tmp_path fixture for all filesystem operations.
   Each module writes to its own subdirectory inside tmp_path.

2. Mock ONLY urllib.request.urlopen for original_module.
   Mock ONLY the HTTP layer for migrated_module using responses library.

3. The mock response must be realistic — it must include all fields the code
   actually reads (payload, actions, timestamps, end_of_history marker, etc).
   Study the source code carefully to understand what JSON structure is expected.

4. For original_module (urllib + gzip):
   - Response must be gzip-compressed
   - Must include the 9-char prefix that gets stripped internally
   - Use io.BytesIO for the mock file object

   Example:
   import gzip, io, json
   RESPONSE_DATA = {"payload": {"actions": [{"id": "1", "timestamp": "100"}]}, "end_of_history": True}
   raw = ("X" * 9 + json.dumps(RESPONSE_DATA)).encode()
   compressed = gzip.compress(raw)
   buf = io.BytesIO(compressed)
   mock_resp = MagicMock()
   mock_resp.read.side_effect = buf.read
   mock_resp.readable.return_value = True
   mock_resp.seekable.return_value = False
   mock_resp.writable.return_value = False
   mock_resp.__enter__ = lambda s: s
   mock_resp.__exit__ = MagicMock(return_value=False)

5. For migrated_module (requests, no gzip, slice already inside executeRequest):
   - Response body is plain JSON — no gzip, no prefix needed
   - responses library handles the mock

   Example:
   @responses.activate
   def test_e2e_migrated(tmp_path):
       responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                     json=RESPONSE_DATA, status=200)

6. After both scrapers run, read the output file and compare:
   orig_result = json.loads((tmp_path / "orig" / convID / "conversation.json").read_text())
   mig_result  = json.loads((tmp_path / "mig"  / convID / "conversation.json").read_text())
   assert orig_result == mig_result

7. outDir must be a STRING — the original module uses string concatenation internally.
   Always convert tmp_path to str before passing:
   out_dir_orig = str(tmp_path / "orig")
   out_dir_mig  = str(tmp_path / "mig")

8. For migrated_module the executeRequest already strips 9 chars internally (response.text[9:]).
   So the responses mock body MUST include 9 prefix chars so after the strip the result is valid JSON:
   PREFIX = "X" * 9
   responses.add(responses.POST, 'https://...', body=PREFIX + json.dumps(RESPONSE_DATA), status=200)
   DO NOT use json=RESPONSE_DATA for migrated mocks — use body=PREFIX+json.dumps(RESPONSE_DATA).

9. MANDATORY IMPORTS at top of file:
   import urllib.parse
   import urllib.error
   import urllib.request
   import gzip
   import io
   import json
   import os
   import responses
   import requests
   from unittest.mock import MagicMock, patch
   import pytest
   from original_module import ConversationScraper as OriginalConversationScraper
   from migrated_module import ConversationScraper as MigratedConversationScraper

10. Output ONLY valid Python code. No markdown fences, no explanations.

MODULE QUIRKS:
{module_quirks}

ORIGINAL CODE:
{original_code}

MIGRATED CODE:
{migrated_code}
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

    has_any_mock = bool(re.search(r"MagicMock|patch\(|@patch|responses\.", code))
    if not has_any_mock:
        return False, "No mocking found — tests would make real HTTP requests"

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
        failed_tests = [t["nodeid"] for t in tests if t.get("outcome") in ("failed", "error")]
        passed_tests = [t["nodeid"] for t in tests if t.get("outcome") == "passed"]
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
    code = re.sub(
        r"^\s*from\s+util\s+import\s+logger.*$",
        "import logging; logger = logging.getLogger(__name__)",
        code, flags=re.MULTILINE,
    )
    code = re.sub(
        r"^\s*from\s+\.\w+\s+import\s+.*$",
        "# relative import removed for isolation",
        code, flags=re.MULTILINE,
    )
    code = re.sub(
        r"^\s*sys\.path\.append\(.*__file__.*\).*$",
        "# sys.path.append removed for isolation",
        code, flags=re.MULTILINE,
    )
    return code


def _run_pytest_debug_file(tmpdir: Path, filename: str = "test_equivalence.py") -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", filename,
         "-v", "--tb=long", "--no-header"],
        cwd=tmpdir, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    debug_path = Path(__file__).parent / f"debug_{filename.replace('.py', '')}.txt"
    try:
        debug_path.write_text(output, encoding="utf-8")
    except Exception:
        pass
    return output


def _run_pytest_debug(tmpdir: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "test_equivalence.py",
         "-v", "--tb=long", "--no-header"],
        cwd=tmpdir, capture_output=True, text=True, timeout=60,
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
    e2e_test_code:           str
    pytest_summary:          dict
    e2e_pytest_summary:      dict
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
    print("[Generator] Generating unit tests...")

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
        print(f"  [Generator] Attempt {attempt} invalid: {msg}")
        reason = msg

    if not test_code:
        error = True
        print(f"[Generator] WARNING: unit tests failed after {LLM_RETRY_ATTEMPTS} attempts — {reason}")

    # ── Generate E2E tests ────────────────────────────────────────────────────
    print("[Generator] Generating E2E tests...")

    e2e_prompt = (
        PROMPT_E2E
        .replace("{original_code}", state["original_code"])
        .replace("{migrated_code}", state["migrated_code"])
        .replace("{module_quirks}", json.dumps(state.get("module_quirks", {}), indent=2))
    )

    e2e_test_code = ""
    for attempt in range(1, LLM_RETRY_ATTEMPTS + 1):
        raw = _invoke_llm(e2e_prompt, attempts=1)
        valid, msg = _validate_test_code(raw)
        if valid:
            e2e_test_code = raw
            print(f"[Generator] Valid E2E tests on attempt {attempt}")
            try:
                out_dir = Path(__file__).parent / "generated_tests"
                out_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                (out_dir / f"test_e2e_{ts}.py").write_text(e2e_test_code, encoding="utf-8")
                print(f"[Generator] Saved to generated_tests/test_e2e_{ts}.py")
            except Exception:
                pass
            break
        print(f"  [Generator] E2E attempt {attempt} invalid: {msg}")

    if not e2e_test_code:
        print("[Generator] WARNING: E2E test generation failed — continuing with unit tests only")

    return {**state, "test_code": test_code, "e2e_test_code": e2e_test_code,
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
            raise RuntimeError(
                f"Missing: {pkg}. Run: pip install pytest pytest-json-report pytest-cov responses"
            )

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

    print(f"[Executor] Unit tests done — original: {s_orig['passed']}p/{s_orig['failed']}f "
          f"| migrated: {s_mig['passed']}p/{s_mig['failed']}f")

    # ── Run E2E tests ─────────────────────────────────────────────────────────
    e2e_summary = {"original": {}, "migrated": {}}
    if state.get("e2e_test_code"):
        print("[Executor] Running E2E tests...")
        with tempfile.TemporaryDirectory() as e2e_tmp:
            e = Path(e2e_tmp)
            e.joinpath("original_module.py").write_text(_sanitize_code(state["original_code"]), encoding="utf-8")
            e.joinpath("migrated_module.py").write_text(_sanitize_code(state["migrated_code"]), encoding="utf-8")
            e.joinpath("test_e2e.py").write_text(state["e2e_test_code"], encoding="utf-8")

            e2e_debug = _run_pytest_debug_file(e, "test_e2e.py")
            print(f"  [Executor] E2E preview:\n{e2e_debug[:600]}\n  ...")

            def run_e2e(module: str) -> tuple[dict, float]:
                rjson = e / f"e2e_report_{module}.json"
                cjson = e / f"e2e_cov_{module}.json"
                subprocess.run(
                    [sys.executable, "-m", "pytest", "test_e2e.py",
                     "-v", "--tb=short",
                     "--json-report", f"--json-report-file={rjson}",
                     f"--cov={module}", f"--cov-report=json:{cjson}"],
                    cwd=e, capture_output=True, text=True, timeout=120,
                )
                return _parse_pytest_json(rjson), _parse_coverage_json(cjson)

            e2e_orig, e2e_cov_orig = run_e2e("original_module")
            e2e_mig,  e2e_cov_mig  = run_e2e("migrated_module")
            e2e_summary = {
                "original": {**e2e_orig, "coverage": e2e_cov_orig},
                "migrated":  {**e2e_mig,  "coverage": e2e_cov_mig},
            }
            print(f"[Executor] E2E done — original: {e2e_orig['passed']}p/{e2e_orig['failed']}f "
                  f"| migrated: {e2e_mig['passed']}p/{e2e_mig['failed']}f")
    else:
        print("[Executor] Skipping E2E — no E2E tests generated")

    return {
        **state,
        "pytest_summary": {"original": {**s_orig, "coverage": cov_orig},
                           "migrated":  {**s_mig,  "coverage": cov_mig}},
        "e2e_pytest_summary": e2e_summary,
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
                "valid_baseline": 0, "regressions": 0,
                "symmetric_failures": 0, "regression_rate": 0.0,
                "equivalence_rate": 0.0,
            },
            "coverage": state["coverage"],
            "regressions_detected": [],
            "symmetric_failures": [],
            "scores": {"overall": 0.0},
            "status": "FAIL",
            "failure_reason": state.get("generation_error_reason", "unknown"),
            "unreliable_results": True,
        }
        return {**state, "evaluation": evaluation, "regressions": []}

    orig_passed_set = set(orig.get("passed_tests", []))
    mig_failed_set  = set(mig["failed_tests"])
    orig_failed_set = set(orig["failed_tests"])

    # Regressions: passed on original, failed on migrated → real migration issues
    regressions = [t for t in mig_failed_set if t not in orig_failed_set]

    # Symmetric failures: failed on both → generation noise, ignored in scoring
    symmetric_failures = [t for t in mig_failed_set if t in orig_failed_set]

    valid_baseline   = orig["passed"]
    regression_count = len(regressions)
    unreliable       = valid_baseline == 0

    if valid_baseline > 0:
        regression_rate = round((regression_count / valid_baseline) * 100, 2)
        equiv = round(((valid_baseline - regression_count) / valid_baseline) * 100, 2)
    else:
        regression_rate = 0.0
        equiv = 0.0

    status = "PASS" if (equiv >= EQUIVALENCE_THRESHOLD and not unreliable) else "FAIL"

    if unreliable:
        print("[Evaluator] WARNING: 0 tests passed on original — no valid baseline")

    evaluation = {
        "execution_summary": {
            "original_passed":    orig["passed"],
            "original_failed":    orig["failed"],
            "migrated_passed":    mig["passed"],
            "migrated_failed":    mig["failed"],
            "valid_baseline":     valid_baseline,
            "regressions":        regression_count,
            "symmetric_failures": len(symmetric_failures),
            "regression_rate":    regression_rate,
            "equivalence_rate":   equiv,
        },
        "coverage": {
            "original": state["coverage"].get("original", 0.0),
            "migrated": state["coverage"].get("migrated", 0.0),
        },
        "regressions_detected": regressions,
        "symmetric_failures":   symmetric_failures,
        "scores":               {"overall": equiv},
        "status":               status,
        "unreliable_results":   unreliable,
    }

    # ── E2E evaluation ───────────────────────────────────────────────────────
    e2e_summary = state.get("e2e_pytest_summary", {})
    e2e_orig = e2e_summary.get("original", {})
    e2e_mig  = e2e_summary.get("migrated", {})
    e2e_regressions = [
        t for t in e2e_mig.get("failed_tests", [])
        if t not in e2e_orig.get("failed_tests", [])
    ]
    e2e_baseline = e2e_orig.get("passed", 0)
    e2e_equiv = round(
        ((e2e_baseline - len(e2e_regressions)) / max(e2e_baseline, 1)) * 100, 2
    ) if e2e_baseline > 0 else 0.0

    evaluation["e2e"] = {
        "baseline":    e2e_baseline,
        "regressions": len(e2e_regressions),
        "equiv":       e2e_equiv,
        "regressions_detected": e2e_regressions,
    }

    print(
        f"[Evaluator] unit: baseline={valid_baseline} regressions={regression_count} "
        f"noise={len(symmetric_failures)} equiv={equiv:.1f}% | "
        f"e2e: baseline={e2e_baseline} regressions={len(e2e_regressions)} equiv={e2e_equiv:.1f}% | "
        f"status={status}"
        + (" (UNRELIABLE)" if unreliable else "")
    )

    return {**state, "evaluation": evaluation, "regressions": regressions}


# ── Node 6: Report ───────────────────────────────────────────────────────────

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
        "e2e_test_code":           "",
        "pytest_summary":          {},
        "e2e_pytest_summary":      {},
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
    print(f"   Status:            {ev.get('status', 'N/A')}")
    print(f"   Equivalence:       {es.get('equivalence_rate', 'N/A')}%")
    print(f"   Valid baseline:    {es.get('valid_baseline', 'N/A')} tests")
    print(f"   Regressions:       {es.get('regressions', 'N/A')}")
    print(f"   Symmetric noise:   {es.get('symmetric_failures', 'N/A')}")
    print(f"   Coverage original: {ev.get('coverage', {}).get('original', 'N/A')}%")
    print(f"   Coverage migrated: {ev.get('coverage', {}).get('migrated', 'N/A')}%")
    if result.get("generation_error"):
        print(f"   ⚠️  Generation error: {result.get('generation_error_reason')}")
    e2e = ev.get("e2e", {})
    if e2e:
        print(f"   E2E baseline:      {e2e.get('baseline', 'N/A')} tests")
        print(f"   E2E regressions:   {e2e.get('regressions', 'N/A')}")
        print(f"   E2E equivalence:   {e2e.get('equiv', 'N/A')}%")
    if ev.get("unreliable_results"):
        print("   ⚠️  Results unreliable: no valid baseline (0 tests passed on original)")