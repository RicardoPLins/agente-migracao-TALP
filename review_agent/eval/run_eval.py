#!/usr/bin/env python3
"""
Avaliação isolada do review_agent (REVIEW_AGENT_DIR).

Não executa migration_agent nem test_agent.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
REVIEW_DIR = EVAL_DIR.parent
REPO_ROOT = REVIEW_DIR.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(REVIEW_DIR / ".env", override=False)
        load_dotenv(REPO_ROOT / ".env", override=False)
    except ImportError:
        pass


def _load_review_module():
    # Eval: cobertura completa dos 3 especialistas (não depende do classificador LLM)
    os.environ.setdefault("REVIEW_FORCE_ALL_AGENTS", "1")
    spec = importlib.util.spec_from_file_location(
        "review_agent", REVIEW_DIR / "review-agent.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _read_pair(task: dict[str, Any]) -> tuple[str, str]:
    original = (REVIEW_DIR / task["original"]).read_text(encoding="utf-8")
    migrado = (REVIEW_DIR / task["migrado"]).read_text(encoding="utf-8")
    return original, migrado


def run_single(mod, task: dict[str, Any], run_id: int) -> dict:
    original, migrado = _read_pair(task)
    result = mod._executar_grafo(original, migrado)
    return {
        "task_id": task["id"],
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agentes_acionados": result.get("agentes_acionados", []),
        "achados_estruturados": result.get("achados_estruturados", []),
        "achados_semantica": result.get("achados_semantica", []),
        "achados_seguranca": result.get("achados_seguranca", []),
        "achados_lint": result.get("achados_lint", []),
        "deve_reprocessar": result.get("deve_reprocessar", False),
        "iteracoes": result.get("iteracoes", 0),
        "relatorio_final": result.get("relatorio_final", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Avaliação do review_agent contra gold labels (somente REVIEW_AGENT_DIR)."
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=EVAL_DIR / "gold" / "v1.json",
        help="Arquivo gold JSON",
    )
    parser.add_argument(
        "--task",
        action="append",
        dest="tasks",
        help="ID da tarefa (repita para várias). Default: todas do gold.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Execuções independentes por tarefa (m runs)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=EVAL_DIR / "results",
        help="Diretório de saída",
    )
    parser.add_argument(
        "--score-only",
        action="store_true",
        help="Só pontua JSONs já salvos em --output (sem chamar LLM)",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(EVAL_DIR))
    from score import aggregate_task_runs, format_report, load_gold, score_task

    gold = load_gold(args.gold)
    task_ids = args.tasks or list(gold["tasks"].keys())

    mod = None
    if not args.score_only:
        _load_dotenv()
        mod = _load_review_module()

    args.output.mkdir(parents=True, exist_ok=True)
    exit_code = 0

    for task_id in task_ids:
        if task_id not in gold["tasks"]:
            print(f"ERRO: tarefa '{task_id}' não está em {args.gold}")
            exit_code = 1
            continue

        gold_task = gold["tasks"][task_id]
        task_dir = args.output / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        per_run_scores: list[dict] = []

        for run_id in range(args.runs):
            out_path = task_dir / f"run_{run_id:02d}.json"

            if args.score_only:
                if not out_path.exists():
                    print(f"AVISO: {out_path} não existe — pulando")
                    continue
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            else:
                assert mod is not None
                print(f"[{task_id}] run {run_id + 1}/{args.runs} …")
                payload = run_single(mod, gold_task, run_id)
                out_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            scored = score_task(payload, gold_task)
            scored["run_id"] = run_id
            per_run_scores.append(scored)
            score_path = task_dir / f"run_{run_id:02d}.score.json"
            score_path.write_text(
                json.dumps(scored, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        if not per_run_scores:
            continue

        agg = aggregate_task_runs(per_run_scores)
        report = format_report(task_id, per_run_scores, agg)
        (task_dir / "summary.md").write_text(report, encoding="utf-8")
        (task_dir / "aggregate.json").write_text(
            json.dumps(agg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(report)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
