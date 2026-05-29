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
    model="llama-3.3-70b-versatile",
    temperature=0.0,
    max_tokens=4096,
)

# ── Prompts ───────────────────────────────────────────────────────────────────

PROMPT_ANALYZER = """
You are a test planning analyst for Python migration projects.

Your task: analyze a urllib-based module (ORIGINAL) and its requests-based rewrite (MIGRATED),
then produce a structured test plan to verify behavioral equivalence.

## Output
Return ONLY valid JSON matching the schema below.
No markdown fences, no explanation, no comments.

## Schema
{
  "original_functions": [
    {
      "name": str,
      "params": [str],
      "returns": str,          // "bytes" | "dict" | "str" | "None" | other
      "raises": [str]          // e.g. ["urllib.error.URLError", "urllib.error.HTTPError"]
    }
  ],
  "migrated_functions": [
    {
      "name": str,
      "params": [str],
      "returns": str,          // "bytes" | "dict" | "str" | "None" | other
      "raises": [str]          // e.g. ["requests.exceptions.ConnectionError", "requests.exceptions.HTTPError"]
    }
  ],
  "equivalence_pairs": [
    {
      "original": str,         // function name or "NONE"
      "migrated": str,         // function name or "NONE"
      "mapping": str,          // "direct" | "split" | "merged" | "removed" | "added"
      "behavioral_diff": str   // "none" | "gzip_handling" | "raise_for_status" | "return_type" | "encoding" | "multiple"
    }
  ],
  "test_scenarios": [
    {
      "id": str,               // "TC001", "TC002", ...
      "category": str,         // see Category Rules
      "function_pair": str,    // must match a value in equivalence_pairs[].original
      "description": str,      // one sentence: what behavioral property this test verifies
      "inputs": {},            // concrete argument values, e.g. {"user_id": 42} — NOT mock code
      "expected_behavior": str // "same" | "original_raises_url_error" | "original_raises_http_error" | "migrated_raises_http_error" | "different_return_type"
      "priority": str          // "high" | "medium" | "low"
    }
  ],
  "coverage_notes": str        // one sentence
}

## Category Rules
Allowed: "happy_path" | "http_error" | "network_error" | "invalid_input" | "headers" | "response_parsing"
Minimum required:
  - happy_path: 2
  - http_error: 2
  - network_error: 1
  - invalid_input: 1
Maximum total: 12

## Migration Context
ORIGINAL uses urllib. Keep in mind:
  - Network errors raise urllib.error.URLError
  - HTTP errors raise urllib.error.HTTPError
  - Responses must be manually decoded (read() + decode())
  - Gzip must be decompressed manually via gzip.GzipFile

MIGRATED uses requests. Keep in mind:
  - Network errors raise requests.exceptions.ConnectionError
  - HTTP errors only raise if raise_for_status() is explicitly called
  - Response body is accessed via .text or .json()
  - Gzip is decompressed automatically

## Constraints
  - "inputs" must contain concrete values — never mock setup or code
  - "function_pair" must reference a value that exists in equivalence_pairs[].original
  - If a function has no counterpart, use "NONE" in that field
  - If you cannot determine a value with certainty, use null — do not guess
  - Do not invent function names that do not exist in the source code

ORIGINAL MODULE (urllib):
{original_code}

MIGRATED MODULE (requests):
{migrated_code}
""".strip()


PROMPT_INSPECTOR = """
You are a code inspector specialized in Python HTTP library migrations (urllib → requests).

Your task: analyze both modules and extract implementation details that determine
how tests must be structured and mocked.

## Output
Return ONLY valid JSON matching the schema below.
No markdown fences, no explanation, no comments.

## Schema
{
  "original": {
    "module_style": str,                    // "functions" | "class"
    "main_class_name": str,                 // class name or null if module_style is "functions"
    "uses_gzip": bool,                      // true if response is read through gzip.GzipFile
    "response_strip_chars": int,            // chars stripped before JSON parse, e.g. 9 for data[9:], 0 if none
    "raises_on_http_error": bool,           // true if module explicitly raises on 4xx/5xx
    "raises_on_network_error": bool,        // true if module explicitly raises on connection failure
    "request_builder": {
      "name": str,                          // function/method name that builds request data, or null
      "return_type": str                    // "bytes" | "dict" | "str" | "other" | null
    },
    "local_imports": [str],                 // internal project imports that may cause ImportError
    "missing_imports": [str]                // imports referenced but not defined in the module
  },
  "migrated": {
    "module_style": str,
    "main_class_name": str,
    "uses_gzip": bool,
    "response_strip_chars": int,
    "raises_on_http_error": bool,           // true only if raise_for_status() is explicitly called
    "raises_on_network_error": bool,
    "request_builder": {
      "name": str,
      "return_type": str
    },
    "local_imports": [str],
    "missing_imports": [str]
  },
  "mock_strategy": {
    "original": {
      "http_layer": str,                    // "urllib.request.urlopen" | "urllib.request.Request" | other
      "response_style": str,                // "read_bytes" | "read_gzip"
      "network_error": str,                 // exact exception to raise: "urllib.error.URLError"
      "http_error": str                     // exact exception to raise: "urllib.error.HTTPError"
    },
    "migrated": {
      "http_layer": str,                    // "responses" (library) | "unittest.mock"
      "response_style": str,                // "body_plain" | "body_prefixed" | "json"
      "network_error": str,                 // exact exception: "requests.exceptions.ConnectionError"
      "http_error": str                     // "responses.add with status 4xx/5xx"
    }
  },
  "behavioral_diffs": [
    {
      "aspect": str,                        // "gzip_handling" | "raise_for_status" | "return_type" | "encoding" | "response_parsing"
      "original": str,                      // one sentence describing original behavior
      "migrated": str                       // one sentence describing migrated behavior
    }
  ]
}

## Migration Context
ORIGINAL uses urllib:
  - gzip.GzipFile(fileobj=response) indicates uses_gzip=true
  - data[N:] before json.loads indicates response_strip_chars=N
  - explicit try/except on urllib.error.HTTPError indicates raises_on_http_error=true
  - generateRequestData returning urlencode().encode() means return_type="bytes"

MIGRATED uses requests:
  - raise_for_status() indicates raises_on_http_error=true — absence means false
  - gzip is always automatic — uses_gzip is almost always false
  - generateRequestData returning a dict means return_type="dict"
  - response_strip_chars applies to .text before json.loads, same as original

## Constraints
  - If you cannot determine a value with certainty, use null — do not guess
  - raises_on_http_error for migrated must be false unless raise_for_status() is explicitly present
  - Do not infer behavior that is not explicitly in the source code

ORIGINAL MODULE (urllib):
{original_code}

MIGRATED MODULE (requests):
{migrated_code}
""".strip()

PROMPT_GENERATOR = """
You are a Python test engineer specialized in urllib → requests migration testing.

Your task: generate a single pytest file that proves functional equivalence between
original_module (urllib) and migrated_module (requests).

## Output
Output ONLY valid Python code.
No markdown fences, no explanations, no TODOs, no comments explaining what to do.

## Input data you have
- MODULE QUIRKS: implementation details extracted by a code inspector
- TEST PLAN: scenarios to cover, each with inputs and expected_behavior
- Source code of both modules

## Structure of the generated file

### 1. Imports
Always import:
  import json
  import pytest
  import responses
  import requests
  import requests.exceptions
  from unittest.mock import MagicMock, patch

Import conditionally based on MODULE QUIRKS:
  - If original.uses_gzip is true:  import gzip, import io
  - If original.uses_gzip is false: do NOT import gzip or io

Import the modules under test:
  - If module_style is "class":
      from original_module import {main_class_name} as OriginalClass
      from migrated_module import {main_class_name} as MigratedClass
  - If module_style is "functions":
      from original_module import <only the functions used in tests>
      from migrated_module import <only the functions used in tests>

Always import urllib errors for original mocks:
  import urllib.error
  import urllib.request

### 2. Fixtures
Create pytest fixtures for both module instances.
Read the __init__ signatures from the source code — they WILL differ between modules.
One fixture per class. If module_style is "functions", skip this section.

Example:
  @pytest.fixture
  def original():
      return OriginalClass(param1, param2)

  @pytest.fixture
  def migrated():
      return MigratedClass(param1)

### 3. Tests
One test function per scenario in TEST PLAN.
Name format: test_{id}_{function_pair}_{category}  (e.g. test_TC001_get_user_happy_path)

## Mocking rules

### Original module (urllib)
Use mock_strategy.original from MODULE QUIRKS.

If original.uses_gzip is true, mock MUST serve real gzip bytes:
  PAYLOAD = {{...}}
  compressed = gzip.compress(json.dumps(PAYLOAD).encode())
  buf = io.BytesIO(compressed)
  mock_resp = MagicMock()
  mock_resp.read.side_effect = buf.read    # side_effect=callable, NOT return_value
  mock_resp.readable.return_value = True
  mock_resp.seekable.return_value = False
  mock_resp.writable.return_value = False
  mock_resp.__enter__ = lambda s: s
  mock_resp.__exit__ = MagicMock(return_value=False)

If original.uses_gzip is false:
  mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')

For network errors:
  side_effect = urllib.error.URLError("connection refused")

For HTTP errors:
  side_effect = urllib.error.HTTPError(url, status_code, "reason", {{}}, None)

### Migrated module (requests)
Use mock_strategy.migrated from MODULE QUIRKS.
Always use @responses.activate decorator — NEVER as context manager.

If migrated.response_strip_chars > 0, body MUST include the prefix:
  PREFIX = "X" * {response_strip_chars}
  responses.add(responses.POST, url, body=PREFIX + json.dumps(PAYLOAD), status=200)
  # DO NOT use json=PAYLOAD when strip_chars > 0

If migrated.response_strip_chars == 0:
  responses.add(responses.POST, url, json=PAYLOAD, status=200)

For network errors:
  responses.add(responses.POST, url, body=requests.exceptions.ConnectionError())

For HTTP errors:
  responses.add(responses.POST, url, status=404)

## Assert rules — CRITICAL
This is the most important section. Weak asserts invalidate the entire equivalence test.

### Rule A — Always assert the return value, never just execution
  # WRONG — proves nothing
  original.get_user(1)

  # WRONG — too weak
  assert result is not None

  # CORRECT — proves the value
  assert result == {{"id": 1, "name": "Alice"}}

### Rule B — For functions with different return types, normalize before comparing
request_builder in original returns bytes, in migrated returns dict.
Normalize to dict for comparison:
  import urllib.parse
  orig_bytes = original.{request_builder_name}(inputs)
  orig_dict  = dict(urllib.parse.parse_qsl(orig_bytes.decode()))
  mig_dict   = migrated.{request_builder_name}(inputs)
  assert orig_dict == mig_dict

### Rule C — For error scenarios, assert the specific exception type and message
  # WRONG
  with pytest.raises(Exception):
      ...

  # CORRECT for original
  with pytest.raises(urllib.error.URLError, match="connection refused"):
      ...

  # CORRECT for migrated (only if raises_on_http_error is true in MODULE QUIRKS)
  with pytest.raises(requests.exceptions.HTTPError):
      ...

### Rule D — Assert raises on migrated ONLY if raises_on_http_error is true
If migrated.raises_on_http_error is false, assert the function returns normally
even on 4xx/5xx — do NOT assert raises.

### Rule E — For response parsing tests, assert specific fields, not the whole object
  # WRONG
  assert result == response

  # CORRECT
  assert result["id"] == 42
  assert result["status"] == "active"

## What to test
Good candidates (pure logic or mockable HTTP):
  - request_builder function (no mocking needed — pure function)
  - executeRequest or equivalent (mock HTTP layer)
  - __init__ attribute assignment

Skip these (too many side effects):
  - Functions that write to filesystem (open, write, os.path)
  - Functions that call sys.exit
  - Orchestrator functions that only call other already-tested functions

MODULE QUIRKS:
{module_quirks}

TEST PLAN:
{test_plan}

ORIGINAL MODULE (urllib):
{original_code}

MIGRATED MODULE (requests):
{migrated_code}
""".strip()

PROMPT_REPORT = """
You are a senior engineering reviewer writing a migration equivalence report.


Output ONLY valid Markdown. No preamble, no explanation outside the report.

---

# Equivalence Report — urllib → requests Migration

**Generated:** {timestamp}

---

## Verdict: [APPROVED / CONDITIONAL / REJECTED]

> [One sentence: what this means for the migration PR — can it be merged, does it need fixes, or is it blocked?]

---

## Test Results

| Metric | Value |
|---|---|
| Valid baseline (original passing) | N |
| Confirmed equivalent (pass on both) | N |
| Regressions (pass original → fail migrated) | N |
| Inversions (fail original → pass migrated) | N |
| Symmetric failures (fail on both — noise) | N |
| Equivalence rate | N% |

---

## Behavioral Analysis

###  Regressions — Migration Broke These
<!-- If none: write "None detected." -->
<!-- If any: list each test ID + one sentence on WHAT likely broke and WHERE to look -->
<!-- Example:
- `test_TC003_get_user_network_error` — network error no longer raises on migrated; check if raise_for_status() is missing
- `test_TC007_parse_response_http_error` — 4xx response silently returns instead of raising; migrated may lack error handling
-->

### Inversions — Migrated Behaves Differently (Review Required)
<!-- If none: write "None detected." -->
<!-- For each: classify as one of: [likely bug fix | behavior change | test artifact] + one sentence why -->
<!-- Example:
- `test_TC005_gzip_response` — [behavior change] original required manual gzip decompression; migrated handles it automatically
-->

### Symmetric Failures — Noise (Both Failed, Ignored)
<!-- If none: write "None." -->
<!-- List test IDs only — these are generation artifacts, not migration issues -->

---

## Actionable Recommendations
<!-- Only include this section if verdict is CONDITIONAL or REJECTED -->
<!-- Be specific: name the function, the behavior, and what to check -->
<!-- Example:
1. `executeRequest()` — verify that `raise_for_status()` is called after every POST; original raised on 4xx via HTTPError
2. `parseResponse()` — confirm strip logic (`data[9:]`) is preserved; migrated uses `.text` which may include the prefix
-->

---

## Coverage

| Module | Line Coverage |
|---|---|
| Original (urllib) | N% |
| Migrated (requests) | N% |

>  Coverage measures code execution, not behavioral correctness. A module can have 100% coverage and still return wrong values.

---

## Warnings
<!-- Unreliable baseline, generation errors, or "None." -->

---

## Verdict Rules Applied
- **APPROVED** — equivalence_rate ≥ 95% AND valid_baseline ≥ 3
- **CONDITIONAL** — equivalence_rate ≥ 85% AND valid_baseline ≥ 3 (merge after fixing regressions)
- **REJECTED** — equivalence_rate < 85% OR valid_baseline < 3 (do not merge)

---

EVALUATION DATA:
{final_evaluation}
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
                print("  [LLM] Daily token limit reached — stopping retries")
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
            "module_style": "functions",
            "main_class_name": None,
        },
        "migrated": {
            "uses_gzip": False, "response_strip_chars": 0,
            "raises_on_http_error": False, "raises_on_network_error": False,
            "generateRequestData_return_type": "unknown",
            "local_imports": [], "missing_imports": [],
            "module_style": "functions",
            "main_class_name": None,
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
