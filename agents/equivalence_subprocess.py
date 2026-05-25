from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
import json
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEST_AGENT_SCRIPT = _PROJECT_ROOT / "test_agent" / "agent" / "agent.py"
_TEST_AGENT_VENV_PY = _PROJECT_ROOT / "test_agent" / "agent" / ".venv" / "bin" / "python"


def _pick_python() -> str:
    if _TEST_AGENT_VENV_PY.exists():
        return str(_TEST_AGENT_VENV_PY)
    return sys.executable


def run_equivalence(original_code: str, migrated_code: str, *, timeout_s: int = 300) -> dict[str, Any]:
    original_code = (original_code or "").strip()
    migrated_code = (migrated_code or "").strip()
    if not original_code or not migrated_code:
        raise ValueError("original_code and migrated_code must be non-empty")

    python_exe = _pick_python()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        original_path = tmpdir / "original.py"
        migrated_path = tmpdir / "migrated.py"
        report_path = tmpdir / "report.md"

        original_path.write_text(original_code)
        migrated_path.write_text(migrated_code)

        cmd = [
            python_exe,
            str(_TEST_AGENT_SCRIPT),
            "--original",
            str(original_path),
            "--migrated",
            str(migrated_path),
            "--output",
            str(report_path),
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=int(timeout_s))

        report_text = report_path.read_text() if report_path.exists() else ""

        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "report": report_text,
        }


def run_equivalence_from_review_output(review_output: dict[str, Any], *, timeout_s: int = 300) -> dict[str, Any]:
    """Run test_agent consuming the JSON produced by review_agent."""
    if not isinstance(review_output, dict):
        raise ValueError("review_output must be a dict")

    python_exe = _pick_python()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "review_output.json"
        report_path = tmpdir / "report.md"

        input_path.write_text(json.dumps(review_output, ensure_ascii=False, indent=2))

        cmd = [
            python_exe,
            str(_TEST_AGENT_SCRIPT),
            "--input-json",
            str(input_path),
            "--output",
            str(report_path),
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=int(timeout_s))
        report_text = report_path.read_text() if report_path.exists() else ""

        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "report": report_text,
        }
