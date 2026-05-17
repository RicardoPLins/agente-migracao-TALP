"""
Test Equivalence Agent
LangGraph pipeline: Analyzer → Generator → Executor → Evaluator (loop) → Report
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

MAX_ITERATIONS = 3
COVERAGE_THRESHOLD = 80.0
EQUIVALENCE_THRESHOLD = 90.0

llm = ChatOpenAI(
    model="qwen/qwen3-32b",
    base_url=os.getenv("PROVIDER_BASE_URL"),
    api_key=os.getenv("PROVIDER_API_KEY"),
)

# ── Paths to prompts ──────────────────────────────────────────────────────────

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

def load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text()


def clean_llm_response(raw: str) -> str:
    """Remove <think> blocks, markdown fences e whitespace do response do LLM."""
    import re
    # Remove bloco <think>...</think> do Qwen3
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = raw.strip()
    # Remove markdown fences ```json ... ``` ou ``` ... ```
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        elif raw.startswith("python"):
            raw = raw[6:]
    raw = raw.strip().rstrip("```").strip()
    return raw

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
    iteration = state.get("iteration", 0)
    print(f"[Generator] Generating tests (iteration {iteration})...")

    extra = ""
    if iteration > 0 and state.get("evaluation"):
        instructions = state["evaluation"].get("generator_instructions", "")
        if instructions:
            extra = f"\n\n## Additional instructions from evaluator (iteration {iteration}):\n{instructions}"

    prompt = load_prompt("node2_generator.txt")
    prompt = prompt.replace("{original_code}", state["original_code"])
    prompt = prompt.replace("{migrated_code}", state["migrated_code"])
    prompt = prompt.replace("{test_plan}", json.dumps(state["test_plan"], indent=2))
    prompt += extra

    response = llm.invoke([HumanMessage(content=prompt)])
    raw = clean_llm_response(response.content)

    return {**state, "test_code": raw, "iteration": iteration}


# ── Node 3: Executor ─────────────────────────────────────────────────────────

def node_executor(state: AgentState) -> AgentState:
    print("[Executor] Running tests...")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        (tmpdir / "original_module.py").write_text(state["original_code"])
        (tmpdir / "migrated_module.py").write_text(state["migrated_code"])
        (tmpdir / "test_equivalence.py").write_text(state["test_code"])

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

def node_evaluator(state: AgentState) -> AgentState:
    print("[Evaluator] Evaluating results...")

    prompt = load_prompt("node4_evaluator.txt")
    prompt = prompt.replace("{pytest_output_original}", state["pytest_output_original"])
    prompt = prompt.replace("{pytest_output_migrated}", state["pytest_output_migrated"])
    prompt = prompt.replace("{coverage_report}", state["coverage_report"])
    prompt = prompt.replace("{test_plan}", json.dumps(state["test_plan"], indent=2))
    prompt = prompt.replace("{iteration_count}", str(state.get("iteration", 0)))

    response = llm.invoke([HumanMessage(content=prompt)])
    raw = clean_llm_response(response.content)

    evaluation = json.loads(raw)
    decision = evaluation.get("decision", "FINALIZE")

    print(f"[Evaluator] Decision: {decision} | "
          f"Coverage: {evaluation['coverage']['original_line_coverage']}% | "
          f"Equivalence: {evaluation['execution_summary']['equivalence_rate']}%")

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


# ── Mock de entrada (temporário até integração com os outros agentes) ─────────

MOCK_ORIGINAL = """
import urllib.request
import json

def get_user(user_id: int) -> dict:
    url = f"https://api.example.com/users/{user_id}"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode())
"""

MOCK_MIGRATED = """
import requests

def get_user(user_id: int) -> dict:
    response = requests.get(
        f"https://api.example.com/users/{user_id}",
        timeout=10
    )
    response.raise_for_status()
    return response.json()
"""


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run equivalence test agent")
    parser.add_argument("--original", help="Path to original (urllib) Python file")
    parser.add_argument("--migrated", help="Path to migrated (requests) Python file")
    parser.add_argument("--mock", action="store_true", help="Use mock input (integration not ready yet)")
    parser.add_argument("--output", default="report.md", help="Output report path")
    args = parser.parse_args()

    if args.mock:
        original = MOCK_ORIGINAL
        migrated = MOCK_MIGRATED
        print("[main] Using mock input")
    else:
        if not args.original or not args.migrated:
            parser.error("--original and --migrated are required when not using --mock")
        original = Path(args.original).read_text()
        migrated = Path(args.migrated).read_text()

    result = run_agent(original, migrated)

    Path(args.output).write_text(result["report"])
    print(f"\n Report saved to {args.output}")
    print(f"   Overall score: {result['evaluation']['scores']['overall']}")