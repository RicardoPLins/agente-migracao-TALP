from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, List, TypedDict

from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph


class CodeReviewState(TypedDict, total=False):
    original_code: str
    migrated_code: str
    semantic_inference: Any
    initial_analysis: str
    issues: List[str]
    final_report: str


class SimpleCodeReviewAgent:
    def __init__(self, *, model_name: str = "llama-3.3-70b-versatile", temperature: float = 0.0):
        # Use local Ollama (no API keys needed, free)
        self.llm = ChatOllama(model="llama3", temperature=temperature)
        self.graph = self._build_graph()

    def _analysis_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        semantic = state.get("semantic_inference")
        original = state.get("original_code", "")
        migrated = state.get("migrated_code", "")

        prompt = (
            "You are a senior Python reviewer specialized in code migrations (urllib -> requests).\n\n"
            "Context (semantic inference to preserve behavior):\n"
            f"{semantic}\n\n"
            "Original code (urllib):\n"
            f"{original}\n\n"
            "Migrated code (requests):\n"
            f"{migrated}\n\n"
            "Analyze the migration briefly. Focus on: preserved behavior, correctness, safety (timeouts/errors), and style."
        )
        response = self.llm.invoke(prompt)
        return {"initial_analysis": response.content}

    def _find_issues(self, state: dict[str, Any]) -> dict[str, Any]:
        migrated = state.get("migrated_code", "")
        initial_analysis = state.get("initial_analysis") or "No prior analysis available."
        prompt = (
            f"Based on the analysis: {initial_analysis}\n\n"
            "Find issues in the migrated code. Be specific and actionable.\n"
            "List 3-7 issues. Format each as '- issue'.\n\n"
            f"Migrated code:\n{migrated}"
        )
        response = self.llm.invoke(prompt)
        issues = [
            line.strip()
            for line in response.content.split("\n")
            if line.strip().startswith("-")
        ]
        return {"issues": issues}

    def _generate_report(self, state: dict[str, Any]) -> dict[str, Any]:
        issues_text = "\n".join(state.get("issues", []) or [])
        initial_analysis = state.get("initial_analysis") or "No analysis available."
        prompt = (
            "Create a migration code review report.\n\n"
            f"Analysis:\n{initial_analysis}\n\n"
            f"Issues:\n{issues_text}\n\n"
            "Format exactly with headings: Summary, Issues, Recommendation."
        )
        response = self.llm.invoke(prompt)
        return {"final_report": response.content}

    def _build_graph(self):
        workflow = StateGraph(CodeReviewState)
        workflow.add_node("analyzer", self._analysis_agent)
        workflow.add_node("issue_finder", self._find_issues)
        workflow.add_node("report_generator", self._generate_report)
        workflow.set_entry_point("analyzer")
        workflow.add_edge("analyzer", "issue_finder")
        workflow.add_edge("issue_finder", "report_generator")
        workflow.add_edge("report_generator", END)
        return workflow.compile()


@lru_cache(maxsize=1)
def _get_agent() -> SimpleCodeReviewAgent:
    return SimpleCodeReviewAgent()


def run_review(code: str) -> dict[str, Any]:
    raise RuntimeError("run_review(code) is deprecated. Use run_migration_review(...).")


def run_migration_review(
    *,
    original_code: str,
    migrated_code: str,
    semantic_inference: Any = None,
) -> dict[str, Any]:
    """Review the migration using original code + migrated code + semantic inference.

    Output contract (for test_agent input):
      - original_code
      - migrated_code
      - semantic_inference
      - review: {analysis, issues, report}
    """
    original_code = (original_code or "").strip()
    migrated_code = (migrated_code or "").strip()
    if not original_code:
        raise ValueError("original_code must be a non-empty string")
    if not migrated_code:
        raise ValueError("migrated_code must be a non-empty string")

    agent = _get_agent()
    final = agent.graph.invoke(
        {
            "original_code": original_code,
            "migrated_code": migrated_code,
            "semantic_inference": semantic_inference,
            "initial_analysis": "",
            "issues": [],
            "final_report": "",
        }
    )

    return {
        "original_code": original_code,
        "migrated_code": migrated_code,
        "semantic_inference": semantic_inference,
        "review": {
            "analysis": final.get("initial_analysis", ""),
            "issues": final.get("issues", []) or [],
            "report": final.get("final_report", ""),
        },
    }
