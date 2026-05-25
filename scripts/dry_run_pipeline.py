#!/usr/bin/env python3
"""Dry-run pipeline that mocks migration → review → test outputs.

This script doesn't call external LLMs; it produces representative outputs
so you can inspect the data flow and formats used by the gateway.
"""
import json
from pathlib import Path


MOCK_ORIGINAL = '''
import urllib.request
import json

def get_user(user_id: int) -> dict:
    url = f"https://api.example.com/users/{user_id}"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode())
'''

MOCK_MIGRATED = '''
import requests

def get_user(user_id: int) -> dict:
    response = requests.get(
        f"https://api.example.com/users/{user_id}",
        timeout=10
    )
    response.raise_for_status()
    return response.json()
'''


def fake_migration(code: str, examples: int = 3) -> dict:
    return {
        "status": "migrado",
        "messages": ["🔄 [mock] migration completed"],
        "original_code": MOCK_ORIGINAL,
        "migrated_code": MOCK_MIGRATED,
    }


def fake_review(original: str, migrated: str, inference: dict | None) -> dict:
    analysis = (
        "The migration preserves the HTTP call and decoding semantics. "
        "Consider adding response.raise_for_status() and explicit exception handling."
    )
    issues = [
        "- Missing explicit exception handling for network errors",
        "- Consider using response.raise_for_status() before json()",
    ]
    report = (
        "Summary:\nMigration looks correct for common GET patterns.\n\n"
        "Issues:\n" + "\n".join(issues) + "\n\nRecommendation:\nApply minor fixes."
    )
    return {
        "original_code": original,
        "migrated_code": migrated,
        "semantic_inference": inference,
        "review": {"analysis": analysis, "issues": issues, "report": report},
    }


def fake_test_result(review_output: dict) -> dict:
    report_md = (
        "# Equivalence Test Report\n\n"
        "- Equivalence: 95%\n"
        "- Coverage (original): 92%\n"
        "- Coverage (migrated): 90%\n"
    )
    return {
        "returncode": 0,
        "stdout": "[mock] tests ran successfully",
        "stderr": "",
        "report": report_md,
        "metrics": {"equivalence": 95.0, "coverage_original": 92.0, "coverage_migrated": 90.0},
    }


def main():
    out_dir = Path("./.dry_run_output")
    out_dir.mkdir(exist_ok=True)

    print("[dry-run] Running mock migration...\n")
    mig = fake_migration(MOCK_ORIGINAL, examples=3)
    print(json.dumps(mig, indent=2, ensure_ascii=False)[:1000])

    print("\n[dry-run] Running mock review...\n")
    review = fake_review(mig["original_code"], mig["migrated_code"], None)
    print(json.dumps(review, indent=2, ensure_ascii=False)[:1200])

    print("\n[dry-run] Running mock tests...\n")
    test = fake_test_result(review)
    print(test["report"])  # already markdown

    # Save combined pipeline output
    combined = {"migration": mig, "review": review, "test": test}
    (out_dir / "pipeline_output.json").write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    (out_dir / "test_report.md").write_text(test["report"])

    print(f"\n[dry-run] Outputs saved to {out_dir.resolve()}\n")


if __name__ == "__main__":
    main()
