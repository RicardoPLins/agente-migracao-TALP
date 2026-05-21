#!/usr/bin/env python3
"""Run the real pipeline (migration -> test -> review) using live LLMs.

Prerequisites:
- Set `GROQ_API_KEY` in env for migration+review.
- Set `PROVIDER_BASE_URL` and `PROVIDER_API_KEY` for the test agent LLM (if required).
- Activate the repo venv and install requirements.

This script will read code from an input file (or use `url.py` by default),
call the migration agent, call the test agent CLI via subprocess with the
original and migrated Python code, then call the review agent. Outputs are
printed and saved into `./.run_output`.
"""
import json
import os
from pathlib import Path
from datetime import datetime
import sys
from dotenv import load_dotenv

# Ensure repo root is on sys.path when running as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load repository .env automatically
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)

from agents.migration import run_migration
from agents.review import run_migration_review
from agents.equivalence_subprocess import run_equivalence


def load_code(path: Path | str | None) -> str:
    if not path:
        # default to repo url.py
        path = Path(__file__).resolve().parent.parent / "url.py"
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    return path.read_text()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run real pipeline with live LLMs")
    parser.add_argument("--input", help="Path to input urllib Python file (default: url.py)")
    parser.add_argument("--examples", type=int, default=10, help="Number of training examples to use")
    parser.add_argument("--timeout", type=int, default=300, help="Test agent timeout seconds")
    args = parser.parse_args()

    out_dir = Path("./.run_output")
    out_dir.mkdir(exist_ok=True)

    # quick env checks
    groq = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_KEY") or os.getenv("API_KEY")
    provider_api = os.getenv("PROVIDER_API_KEY")
    provider_base = os.getenv("PROVIDER_BASE_URL")

    if not groq:
        print("ERROR: GROQ_API_KEY (or GROQ_KEY/API_KEY) not set in environment. Aborting.")
        sys.exit(2)

    print(f"[{datetime.now().isoformat()}] Running migration with {args.examples} examples...")
    code = load_code(args.input)
    migration_out = run_migration(code, num_examples=args.examples)
    print(json.dumps({k: migration_out.get(k) for k in ("status","messages")}, indent=2, ensure_ascii=False))

    migrated_code = migration_out.get("migrated_code", "")
    semantic_inference = migration_out.get("semantic_inference")

    if not migrated_code or not str(migrated_code).strip():
        partial = {"migration": migration_out, "review": None, "test": None}
        (out_dir / "pipeline_output.json").write_text(json.dumps(partial, indent=2, ensure_ascii=False))
        print("ERROR: migration did not return `migrated_code`. Check GROQ key and model access.")
        print(f"Partial output saved to {out_dir.resolve() / 'pipeline_output.json'}")
        sys.exit(3)

    print(f"[{datetime.now().isoformat()}] Running test agent (may take a while)...")
    test_out = run_equivalence(
        original_code=migration_out["original_code"],
        migrated_code=migrated_code,
        timeout_s=args.timeout,
    )

    print(f"[{datetime.now().isoformat()}] Running review...")
    review_out = run_migration_review(
        original_code=migration_out["original_code"],
        migrated_code=migrated_code,
        semantic_inference=semantic_inference,
    )
    print(json.dumps({"review": {"analysis": review_out["review"]["analysis"]}}, indent=2, ensure_ascii=False))

    combined = {"migration": migration_out, "review": review_out, "test": test_out}
    (out_dir / "pipeline_output.json").write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    if test_out.get("report"):
        (out_dir / "test_report.md").write_text(test_out["report"])

    print(f"\nCompleted. Outputs saved to {out_dir.resolve()}\n")


if __name__ == "__main__":
    main()
