"""
Test Equivalence Agent
LangGraph pipeline: Analyzer → Generator → Executor → Evaluator (loop) → Report
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TypedDict
import importlib.util

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

MAX_ITERATIONS = 3
COVERAGE_THRESHOLD = 80.0
EQUIVALENCE_THRESHOLD = 90.0

api_key = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_KEY") or os.getenv("API_KEY")
# llm = ChatGroq(
#         api_key=api_key,
#         model_name="llama-3.3-70b-versatile",
#         temperature=0.0
#             )

llm = ChatOllama(
    model="llama3",
    temperature=0
)

# ── Paths to prompts ──────────────────────────────────────────────────────────

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def clean_llm_response(raw: str) -> str:
    """Remove <think> blocks, markdown fences and leading/trailing whitespace."""
    # Remove <think>...</think> blocks (Qwen3 / reasoning models)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = raw.strip()

    # FIX 3: handle text before the opening fence (e.g. "Here is the code:\n```python\n...")
    fence_match = re.search(r"```(?:json|python)?\n?(.*?)```", raw, flags=re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Fallback: strip bare fences if present at the very start
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith(("json", "python")):
            raw = raw[raw.index("\n") + 1 :]

    return raw.strip().rstrip("```").strip()


# ── State ─────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    # Inputs
    original_code: str
    migrated_code: str

    # Intermediate
    test_plan: dict
    test_code: str
    pytest_output_original: str
    pytest_output_migrated: str
    coverage_report: str
    evaluation: dict

    # Control
    iteration: int
    decision: str           # "CONTINUE" | "FINALIZE"

    # Output
    report: str


# ── Node 1: Analyzer ──────────────────────────────────────────────────────────

def node_analyzer(state: AgentState) -> AgentState:
    print("[Analyzer] Analyzing both codebases...")

    prompt = load_prompt("node1_analyzer.txt")
    prompt = prompt.replace("{original_code}", state["original_code"])
    prompt = prompt.replace("{migrated_code}", state["migrated_code"])

    response = llm.invoke([HumanMessage(content=prompt)])
    raw = clean_llm_response(response.content)

    test_plan = json.loads(raw)
    print(f"[Analyzer] Found {len(test_plan.get('test_scenarios', []))} scenarios")

    return {**state, "test_plan": test_plan}


# ── Node 2: Generator ────────────────────────────────────────────────────────

def node_generator(state: AgentState) -> AgentState:
    # FIX 2: read iteration from state; node_evaluator is responsible for
    # incrementing it *after* this node runs, so here we always see the
    # correct value for the current iteration.
    iteration = state.get("iteration", 0)
    print(f"[Generator] Generating tests (iteration {iteration})...")

    extra = ""
    # On subsequent iterations the evaluator has already incremented the
    # counter, so iteration > 0 is the right check.
    if iteration > 0 and state.get("evaluation"):
        instructions = state["evaluation"].get("generator_instructions", "")
        if instructions:
            extra = (
                f"\n\n## Additional instructions from evaluator"
                f" (iteration {iteration}):\n{instructions}"
            )

    prompt = load_prompt("node2_generator.txt")
    prompt = prompt.replace("{original_code}", state["original_code"])
    prompt = prompt.replace("{migrated_code}", state["migrated_code"])
    prompt = prompt.replace("{test_plan}", json.dumps(state["test_plan"], indent=2))
    prompt += extra

    response = llm.invoke([HumanMessage(content=prompt)])
    raw = clean_llm_response(response.content)

    # Do NOT increment iteration here — node_evaluator owns that counter.
    return {**state, "test_code": raw}


# ── Node 3: Executor ─────────────────────────────────────────────────────────

def node_executor(state: AgentState) -> AgentState:
    print("[Executor] Running tests...")
    # Check that pytest is available
    if importlib.util.find_spec("pytest") is None:
        msg = (
            "pytest is not installed in the current environment.\n"
            "Please install test dependencies, e.g.:\n"
            "  pip install pytest pytest-cov\n"
            "or\n"
            "  pip install -r requirements.txt\n"
        )
        print("[Executor] ", msg)
        pytest_out_original = "ERROR: pytest not installed\n" + msg
        pytest_out_migrated = pytest_out_original
        coverage_report = pytest_out_original
        return {
            **state,
            "pytest_output_original": pytest_out_original,
            "pytest_output_migrated": pytest_out_migrated,
            "coverage_report": coverage_report,
        }

    # Create temp dir and write files
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # FIX: explicit UTF-8 encoding to avoid cp1252 issues on Windows
        (tmpdir / "original_module.py").write_text(state["original_code"], encoding="utf-8")
        (tmpdir / "migrated_module.py").write_text(state["migrated_code"], encoding="utf-8")
        (tmpdir / "test_equivalence.py").write_text(state["test_code"], encoding="utf-8")

        def run_pytest(extra_args: list[str]) -> str:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "test_equivalence.py",
                 "-v", "--tb=short", *extra_args],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.stdout + result.stderr

        pytest_out_original = run_pytest([
            "--cov=original_module",
            "--cov-report=term-missing",
        ])

        pytest_out_migrated = run_pytest([
            "--cov=migrated_module",
            "--cov-report=term-missing",
        ])

        coverage_report = (
            "=== ORIGINAL MODULE ===\n" + pytest_out_original +
            "\n=== MIGRATED MODULE ===\n" + pytest_out_migrated
        )

    print("[Executor] Tests complete")

    return {
        **state,
        "pytest_output_original": pytest_out_original,
        "pytest_output_migrated": pytest_out_migrated,
        "coverage_report": coverage_report,
    }


# ── Node 4: Evaluator ────────────────────────────────────────────────────────

def _parse_pytest_counts(output: str) -> dict:
    """Extract pass/fail/error/skip counts from pytest terminal output."""
    if not output:
        return {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}

    def find_num(key: str) -> int:
        m = re.search(rf"(\d+)\s+{key}", output)
        return int(m.group(1)) if m else 0

    passed = find_num("passed")
    failed = find_num("failed")
    errors = find_num("error(?:s)?")
    skipped = find_num("skipped")
    total = passed + failed + errors + skipped
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
    }


def _extract_coverage_percent(output: str) -> float:
    """
    Extract the *total* coverage percentage from a single pytest-cov output block.

    pytest-cov prints a 'TOTAL' line like:
        TOTAL    120     18    85%
    We look for that line first; fall back to the last percentage found.
    """
    if not output:
        return 0.0

    # Preferred: the TOTAL summary line produced by pytest-cov
    total_match = re.search(r"^TOTAL\s+\d+\s+\d+\s+(\d+(?:\.\d+)?)%", output, re.MULTILINE)
    if total_match:
        return float(total_match.group(1))

    # Fallback: last percentage figure in the block (more reliable than first)
    all_pct = re.findall(r"(\d{1,3}(?:\.\d+)?)%", output)
    if all_pct:
        return float(all_pct[-1])

    return 0.0


def node_evaluator(state: AgentState) -> AgentState:
    print("[Evaluator] Evaluating results...")

    pytest_out_original = state.get("pytest_output_original", "")
    pytest_out_migrated = state.get("pytest_output_migrated", "")

    summary_orig = _parse_pytest_counts(pytest_out_original)
    summary_mig = _parse_pytest_counts(pytest_out_migrated)

    # FIX 4: extract coverage separately from each module's own output block
    cov_orig = _extract_coverage_percent(pytest_out_original)
    cov_mig = _extract_coverage_percent(pytest_out_migrated)

    # FIX 1: pass the full raw pytest outputs so the LLM can detect which
    # specific tests passed in one module but failed in the other
    # (needed to compute regressions_detected accurately).
    # FIX 5 (medium): pass the complete test_plan, not just scenario IDs.
    compact = {
        "pytest_original_summary": summary_orig,
        "pytest_migrated_summary": summary_mig,
        "pytest_original_output": pytest_out_original,
        "pytest_migrated_output": pytest_out_migrated,
        "coverage": {"original": cov_orig, "migrated": cov_mig},
        "test_plan": state.get("test_plan", {}),
        "iteration_count": state.get("iteration", 0),
        "thresholds": {
            "coverage": COVERAGE_THRESHOLD,
            "equivalence": EQUIVALENCE_THRESHOLD,
        },
    }

    # FIX 6 (medium): enforce thresholds in the prompt so the LLM uses them,
    # and add a hard guard below that overrides the LLM if the scores are met.
    prompt = (
        "You are an evaluator. Given the data below, return a JSON evaluation with keys:\n"
        "  execution_summary  (dict with passed/failed/equivalence_rate)\n"
        "  coverage           (dict with original and migrated as floats)\n"
        "  regressions_detected  (list of test names that passed for original but failed for migrated)\n"
        "  scores             (dict with coverage_score, equivalence_score, overall)\n"
        "  decision           (\"CONTINUE\" if coverage < {cov_t} OR equivalence_rate < {eq_t}, else \"FINALIZE\")\n"
        "  missing_scenarios  (list)\n"
        "  generator_instructions  (string with guidance for the next iteration, empty if FINALIZE)\n"
        "  iteration_count    (integer)\n\n"
        "Return ONLY valid JSON, no prose, no markdown.\n\n"
        f"INPUT:\n{json.dumps(compact)}"
    )
    prompt = prompt.replace("{cov_t}", str(COVERAGE_THRESHOLD)).replace("{eq_t}", str(EQUIVALENCE_THRESHOLD))

    response = llm.invoke([HumanMessage(content=prompt)])
    raw = clean_llm_response(response.content)

    evaluation = json.loads(raw)

    # ── Hard threshold guard (FIX 6) ─────────────────────────────────────────
    # The LLM might miscalculate; enforce the thresholds ourselves.
    cov_field = evaluation.get("coverage", {})
    orig_cov = cov_field.get("original", cov_orig) if isinstance(cov_field, dict) else cov_orig

    exec_field = evaluation.get("execution_summary", {})
    equiv = (
        exec_field.get("equivalence_rate", 0.0)
        if isinstance(exec_field, dict)
        else 0.0
    )

    meets_thresholds = orig_cov >= COVERAGE_THRESHOLD and equiv >= EQUIVALENCE_THRESHOLD
    decision = "FINALIZE" if meets_thresholds else evaluation.get("decision", "FINALIZE")

    print(
        f"[Evaluator] Decision: {decision} | "
        f"Coverage: {orig_cov:.1f}% (threshold {COVERAGE_THRESHOLD}%) | "
        f"Equivalence: {equiv:.1f}% (threshold {EQUIVALENCE_THRESHOLD}%)"
    )

    # FIX 2 (part 2): increment iteration *here*, after the generator has run,
    # so that the generator always reads the pre-increment value.
    return {
        **state,
        "evaluation": evaluation,
        "decision": decision,
        "iteration": state.get("iteration", 0) + 1,
    }


# ── Node: Report ─────────────────────────────────────────────────────────────

def node_report(state: AgentState) -> AgentState:
    print("[Report] Generating final report...")

    prompt = load_prompt("node_report.txt")
    prompt = prompt.replace("{final_evaluation}", json.dumps(state["evaluation"], indent=2))
    prompt = prompt.replace("{test_plan}", json.dumps(state["test_plan"], indent=2))
    prompt = prompt.replace("{timestamp}", datetime.now().isoformat())

    response = llm.invoke([HumanMessage(content=prompt)])
    report = clean_llm_response(response.content)

    return {**state, "report": report}


# ── Conditional edge ─────────────────────────────────────────────────────────

def should_continue(state: AgentState) -> str:
    if state["decision"] == "CONTINUE" and state.get("iteration", 0) < MAX_ITERATIONS:
        return "generator"
    return "report"


# ── Graph ─────────────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(AgentState)

    g.add_node("analyzer", node_analyzer)
    g.add_node("generator", node_generator)
    g.add_node("executor", node_executor)
    g.add_node("evaluator", node_evaluator)
    g.add_node("report", node_report)

    g.set_entry_point("analyzer")
    g.add_edge("analyzer", "generator")
    g.add_edge("generator", "executor")
    g.add_edge("executor", "evaluator")
    g.add_conditional_edges("evaluator", should_continue, {
        "generator": "generator",
        "report": "report",
    })
    g.add_edge("report", END)

    return g.compile()


# ── Entrypoint ────────────────────────────────────────────────────────────────

def run_agent(original_code: str, migrated_code: str) -> dict:
    """
    Run the test equivalence agent.

    Args:
        original_code: Source code of the urllib-based implementation
        migrated_code: Source code of the requests-based implementation

    Returns:
        Final state dict with keys: report, evaluation, test_code, test_plan
    """
    graph = build_graph()

    initial_state: AgentState = {
        "original_code": original_code,
        "migrated_code": migrated_code,
        "test_plan": {},
        "test_code": "",
        "pytest_output_original": "",
        "pytest_output_migrated": "",
        "coverage_report": "",
        "evaluation": {},
        "iteration": 0,
        "decision": "CONTINUE",
        "report": "",
    }

    final_state = graph.invoke(initial_state)
    return final_state


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run equivalence test agent")
    parser.add_argument("--original", required=True, help="Path to original (urllib) Python file")
    parser.add_argument("--migrated", required=True, help="Path to migrated (requests) Python file")
    parser.add_argument("--output", default="report.md", help="Output report path")
    args = parser.parse_args()

    original = Path(args.original).read_text(encoding="utf-8")
    migrated = Path(args.migrated).read_text(encoding="utf-8")

    result = run_agent(original, migrated)

    Path(args.output).write_text(result["report"], encoding="utf-8")
    print(f"\n Report saved to {args.output}")
    overall = result.get("evaluation", {}).get("scores", {}).get("overall")
    print(f"   Overall score: {overall if overall is not None else 'N/A'}")