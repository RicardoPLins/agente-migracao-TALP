#!/usr/bin/env python3
"""
Run iterative improvement pipeline with feedback loops:

1. Migration → Test → Review
2. If Test fails: retry migration (max 3 times) then proceed
3. If Review identifies errors: retry migration (max 3 times) with review feedback
4. Repeat until robust or max retries exhausted

Flow diagram:
```
migrate --→ test --→ [test_pass?]
              ↑        ├─ NO (retry<3) → migrate (with context)
              |        └─ YES → review
              └────────────────────┘
                    (feedback)

review --→ [review_ok?]
             ├─ NO (retry<3) → migrate (with review feedback)
             └─ YES → finalize output
```
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import TypedDict, Literal
from dotenv import load_dotenv

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)

from langgraph.graph import StateGraph, START, END
from agents.migration import run_migration
from agents.review import run_migration_review
from agents.equivalence_subprocess import run_equivalence


class PipelineState(TypedDict):
    """State for iterative improvement pipeline."""
    original_code: str
    migrated_code: str
    current_code: str  # Code being migrated
    test_result: dict | None
    review_result: dict | None
    test_passed: bool
    review_ok: bool
    test_retry_count: int
    review_retry_count: int
    max_test_retries: int
    max_review_retries: int
    history: list[dict]  # Track all iterations
    migration_context: str  # Context for next migration attempt


def load_code(path: Path | str | None) -> str:
    """Load Python code from file."""
    if not path:
        path = REPO_ROOT / "url.py"
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    return path.read_text()


def migrate_node(state: PipelineState) -> PipelineState:
    """Execute migration agent."""
    print(f"\n[MIGRATE] Iteration {len([h for h in state['history'] if h['stage']=='migrate'])+1}")
    
    migration_prompt = state["current_code"]
    if state["migration_context"]:
        migration_prompt = f"{state['current_code']}\n\n[CONTEXT FOR IMPROVEMENT]:\n{state['migration_context']}"
    
    try:
        migration_out = run_migration(migration_prompt, num_examples=10)
        
        state["original_code"] = migration_out["original_code"]
        state["migrated_code"] = migration_out["migrated_code"]
        state["current_code"] = migration_out["migrated_code"]
        state["test_passed"] = False
        state["review_ok"] = False
        state["migration_context"] = ""
        
        state["history"].append({
            "stage": "migrate",
            "iteration": len([h for h in state["history"] if h["stage"]=="migrate"]),
            "status": migration_out["status"],
            "messages": migration_out.get("messages", [])
        })
        
        print(f"  Status: {migration_out['status']}")
        return state
    except Exception as e:
        print(f"  ERROR: {e}")
        state["history"].append({
            "stage": "migrate",
            "iteration": len([h for h in state["history"] if h["stage"]=="migrate"]),
            "status": "error",
            "error": str(e)
        })
        return state


def test_node(state: PipelineState) -> PipelineState:
    """Execute test agent."""
    print(f"\n[TEST] Iteration {len([h for h in state['history'] if h['stage']=='test'])+1}")
    
    try:
        test_out = run_equivalence(
            original_code=state["original_code"],
            migrated_code=state["migrated_code"],
            timeout_s=300
        )
        
        state["test_result"] = test_out
        test_passed = test_out.get("status") == "success"
        state["test_passed"] = test_passed
        
        # Extract failure reason if test failed
        if not test_passed:
            failure_msg = test_out.get("error", test_out.get("report", "Unknown test failure"))
            state["migration_context"] = f"Test failed:\n{failure_msg}\n\nFix these issues and ensure the migrated code is functionally equivalent."
        
        state["history"].append({
            "stage": "test",
            "iteration": len([h for h in state["history"] if h["stage"]=="test"]),
            "status": test_out.get("status", "unknown"),
            "passed": test_passed,
            "summary": test_out.get("summary", "")
        })
        
        print(f"  Status: {'PASSED ✓' if test_passed else 'FAILED ✗'}")
        if not test_passed:
            print(f"  Reason: {state['migration_context'][:150]}...")
        
        return state
    except Exception as e:
        print(f"  ERROR: {e}")
        state["test_passed"] = False
        state["migration_context"] = f"Test agent error: {str(e)}"
        state["history"].append({
            "stage": "test",
            "iteration": len([h for h in state["history"] if h["stage"]=="test"]),
            "status": "error",
            "error": str(e)
        })
        return state


def check_test_result(state: PipelineState) -> Literal["retry_migrate", "proceed_review", "finalize_fail"]:
    """Decide next step after test."""
    if state["test_passed"]:
        return "proceed_review"
    elif state["test_retry_count"] < state["max_test_retries"]:
        print(f"\n[DECISION] Test failed. Retrying migration (attempt {state['test_retry_count']+1}/{state['max_test_retries']})")
        state["test_retry_count"] += 1
        return "retry_migrate"
    else:
        print(f"\n[DECISION] Test failed. Max retries ({state['max_test_retries']}) reached. Proceeding to review anyway.")
        return "proceed_review"


def review_node(state: PipelineState) -> PipelineState:
    """Execute review agent."""
    print(f"\n[REVIEW] Iteration {len([h for h in state['history'] if h['stage']=='review'])+1}")
    
    try:
        review_out = run_migration_review(
            original_code=state["original_code"],
            migrated_code=state["migrated_code"],
            semantic_inference=None
        )
        
        state["review_result"] = review_out
        
        # Check if review found issues
        analysis = review_out.get("review", {}).get("analysis", "")
        issues = review_out.get("review", {}).get("issues", [])
        review_ok = len(issues) == 0 or "no issues" in analysis.lower()
        state["review_ok"] = review_ok
        
        # Extract issues for context
        if not review_ok:
            issues_text = "\n".join([str(i) for i in issues[:5]])
            state["migration_context"] = f"Review identified issues:\n{issues_text}\n\nFix these issues to improve the migrated code."
        
        state["history"].append({
            "stage": "review",
            "iteration": len([h for h in state["history"] if h["stage"]=="review"]),
            "issues_found": len(issues),
            "ok": review_ok,
            "analysis_preview": analysis[:200] if analysis else ""
        })
        
        print(f"  Issues found: {len(issues)}")
        if not review_ok:
            print(f"  Issues: {state['migration_context'][:150]}...")
        
        return state
    except Exception as e:
        print(f"  ERROR: {e}")
        state["review_result"] = None
        state["review_ok"] = False
        state["migration_context"] = f"Review agent error: {str(e)}"
        state["history"].append({
            "stage": "review",
            "iteration": len([h for h in state["history"] if h["stage"]=="review"]),
            "status": "error",
            "error": str(e)
        })
        return state


def check_review_result(state: PipelineState) -> Literal["retry_migrate_review", "finalize_success", "finalize_partial"]:
    """Decide next step after review."""
    if state["review_ok"]:
        print(f"\n[DECISION] Review passed. Code is ready!")
        return "finalize_success"
    elif state["review_retry_count"] < state["max_review_retries"]:
        print(f"\n[DECISION] Review found issues. Retrying migration with review feedback (attempt {state['review_retry_count']+1}/{state['max_review_retries']})")
        state["review_retry_count"] += 1
        return "retry_migrate_review"
    else:
        print(f"\n[DECISION] Review issues remain. Max retries ({state['max_review_retries']}) reached. Finalizing with best effort.")
        return "finalize_partial"


def finalize_node(state: PipelineState, reason: Literal["success", "partial"]) -> PipelineState:
    """Finalize and save output."""
    print(f"\n[FINALIZE] Status: {reason.upper()}")
    print(f"Total iterations: migrate={len([h for h in state['history'] if h['stage']=='migrate'])}, test={len([h for h in state['history'] if h['stage']=='test'])}, review={len([h for h in state['history'] if h['stage']=='review'])}")
    return state


def create_pipeline():
    """Create LangGraph workflow."""
    workflow = StateGraph(PipelineState)
    
    # Add nodes
    workflow.add_node("migrate", migrate_node)
    workflow.add_node("test", test_node)
    workflow.add_node("review", review_node)
    workflow.add_node("finalize_success", lambda s: finalize_node(s, "success"))
    workflow.add_node("finalize_partial", lambda s: finalize_node(s, "partial"))
    
    # Add edges
    workflow.add_edge(START, "migrate")
    workflow.add_edge("migrate", "test")
    workflow.add_conditional_edges("test", check_test_result, {
        "retry_migrate": "migrate",
        "proceed_review": "review",
        "finalize_fail": "finalize_partial"
    })
    workflow.add_conditional_edges("review", check_review_result, {
        "retry_migrate_review": "migrate",
        "finalize_success": "finalize_success",
        "finalize_partial": "finalize_partial"
    })
    workflow.add_edge("finalize_success", END)
    workflow.add_edge("finalize_partial", END)
    
    return workflow.compile()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Run iterative pipeline with feedback loops")
    parser.add_argument("--input", help="Path to input urllib Python file (default: url.py)")
    parser.add_argument("--output-dir", default="./.run_output_iterative", help="Output directory")
    parser.add_argument("--max-test-retries", type=int, default=3, help="Max test retry attempts")
    parser.add_argument("--max-review-retries", type=int, default=3, help="Max review retry attempts")
    args = parser.parse_args()
    
    out_dir = Path(args.output_dir)
    out_dir.mkdir(exist_ok=True)
    
    # Validate environment
    groq = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_KEY") or os.getenv("API_KEY")
    if not groq:
        print("ERROR: GROQ_API_KEY not set. Aborting.")
        sys.exit(2)
    
    # Load initial code
    code = load_code(args.input)
    
    print(f"{'='*60}")
    print(f"ITERATIVE IMPROVEMENT PIPELINE")
    print(f"{'='*60}")
    print(f"Start time: {datetime.now().isoformat()}")
    print(f"Max test retries: {args.max_test_retries}")
    print(f"Max review retries: {args.max_review_retries}")
    
    # Initialize state
    initial_state: PipelineState = {
        "original_code": code,
        "migrated_code": "",
        "current_code": code,
        "test_result": None,
        "review_result": None,
        "test_passed": False,
        "review_ok": False,
        "test_retry_count": 0,
        "review_retry_count": 0,
        "max_test_retries": args.max_test_retries,
        "max_review_retries": args.max_review_retries,
        "history": [],
        "migration_context": ""
    }
    
    # Run pipeline
    pipeline = create_pipeline()
    final_state = pipeline.invoke(initial_state)
    
    # Save results
    result = {
        "status": "success" if final_state["review_ok"] else "partial",
        "duration_minutes": 0,  # Could track time
        "test_passed": final_state["test_passed"],
        "review_ok": final_state["review_ok"],
        "test_retries_used": final_state["test_retry_count"],
        "review_retries_used": final_state["review_retry_count"],
        "original_code": final_state["original_code"],
        "migrated_code": final_state["migrated_code"],
        "test_result": final_state["test_result"],
        "review_result": final_state["review_result"],
        "history": final_state["history"]
    }
    
    output_file = out_dir / "pipeline_output.json"
    output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    
    migrated_file = out_dir / "migrated_code.py"
    migrated_file.write_text(final_state["migrated_code"])
    
    print(f"\n{'='*60}")
    print(f"End time: {datetime.now().isoformat()}")
    print(f"Final Status: {'✓ SUCCESS' if final_state['review_ok'] else '⚠ PARTIAL (best effort)'}")
    print(f"Test Result: {'PASSED ✓' if final_state['test_passed'] else 'FAILED ✗'}")
    print(f"Review Result: {'OK ✓' if final_state['review_ok'] else 'ISSUES FOUND ✗'}")
    print(f"\nOutputs saved to: {out_dir.resolve()}/")
    print(f"  - pipeline_output.json (full details)")
    print(f"  - migrated_code.py (final migrated code)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
