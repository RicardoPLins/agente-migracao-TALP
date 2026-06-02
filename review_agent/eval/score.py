"""
Métricas de avaliação do review_agent contra gold labels.

Uso isolado — não depende do pipeline migration/test.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

_SEV_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def load_gold(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in data.get("tasks", [])}
    return {"version": data.get("version", "?"), "tasks": tasks}


def _texto_achado(a: dict[str, Any]) -> str:
    partes = [
        a.get("descricao") or "",
        a.get("trigger") or "",
        a.get("formatado") or "",
        a.get("raw") or "",
    ]
    return " ".join(partes).lower()


def _severidade_ok(achado: dict[str, Any], minima: str) -> bool:
    sev = achado.get("severidade") or "P3"
    return _SEV_RANK.get(sev, 3) <= _SEV_RANK.get(minima, 1)


def _match_simbolo(achado: dict[str, Any], simbolo: str | None) -> bool:
    if not simbolo:
        return True
    got = achado.get("simbolo") or ""
    if got == simbolo:
        return True
    # Fallback case-insensitive: tolera variação do LLM; gold usa nomes canônicos do código
    return got.lower() == simbolo.lower()


def _match_keywords(texto: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    return any(k.lower() in texto for k in keywords)


def _match_regressao(achado: dict[str, Any], regressao: dict[str, Any]) -> bool:
    if not achado.get("prefixo"):
        return False
    if not _match_simbolo(achado, regressao.get("simbolo")):
        return False
    if not _severidade_ok(achado, regressao.get("severidade_min", "P1")):
        return False
    return _match_keywords(_texto_achado(achado), regressao.get("keywords") or [])


def _match_preexistente(achado: dict[str, Any], item: dict[str, Any]) -> bool:
    if achado.get("severidade") not in ("P0", "P1"):
        return False
    if not _match_simbolo(achado, item.get("simbolo")):
        return False
    return _match_keywords(_texto_achado(achado), item.get("keywords") or [])


def infer_veredito(result: dict[str, Any]) -> str:
    if result.get("deve_reprocessar"):
        return "REPROCESSAR"
    achados = result.get("achados_estruturados") or []
    counts: Counter[str] = Counter()
    for a in achados:
        if a.get("prefixo") and a.get("severidade"):
            counts[a["severidade"]] += 1
    if counts.get("P0", 0) > 0:
        return "REPROVADO"
    if counts.get("P1", 0) > 0:
        return "APROVADO_COM_RESSALVAS"
    return "APROVADO"


def score_task(result: dict[str, Any], gold_task: dict[str, Any]) -> dict[str, Any]:
    achados = result.get("achados_estruturados") or []
    regressoes = gold_task.get("regressoes") or []
    preexistentes = gold_task.get("preexistentes") or []

    hits: list[str] = []
    for reg in regressoes:
        if any(_match_regressao(a, reg) for a in achados):
            hits.append(reg["id"])

    recall_r = len(hits) / len(regressoes) if regressoes else 1.0

    falsos_p1 = 0
    for a in achados:
        if a.get("severidade") not in ("P0", "P1"):
            continue
        if any(_match_regressao(a, reg) for reg in regressoes):
            continue
        if any(_match_preexistente(a, pre) for pre in preexistentes):
            falsos_p1 += 1

    veredito = infer_veredito(result)
    esperado = gold_task.get("veredito_esperado", "APROVADO")
    veredito_ok = veredito == esperado

    strict_pass = recall_r >= 1.0 and falsos_p1 == 0 and veredito_ok

    agentes = result.get("agentes_acionados") or []
    cobertura_3 = set(agentes) >= {"semantica", "seguranca", "lint"}

    return {
        "task_id": gold_task["id"],
        "recall_R": round(recall_r, 4),
        "regressoes_detectadas": hits,
        "regressoes_esperadas": [r["id"] for r in regressoes],
        "falsos_p1_preexistente": falsos_p1,
        "veredito": veredito,
        "veredito_esperado": esperado,
        "veredito_ok": veredito_ok,
        "strict_pass": strict_pass,
        "agentes_acionados": agentes,
        "cobertura_3_agentes": cobertura_3,
        "total_achados": len(achados),
    }


def pass_at_k(successes: int, total_runs: int, k: int) -> float:
    """pass@k para uma tarefa (Chen et al.)."""
    if total_runs < k or total_runs == 0:
        return 0.0
    if successes == 0:
        return 0.0
    fail = math.comb(total_runs - successes, k) / math.comb(total_runs, k)
    return 1.0 - fail


def pass_all_k(successes: int, total_runs: int, k: int) -> float:
    """pass^k — sucesso em todas as k tentativas amostradas."""
    if total_runs < k or total_runs == 0:
        return 0.0
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(total_runs, k)


def aggregate_task_runs(scores: list[dict[str, Any]]) -> dict[str, Any]:
    if not scores:
        return {}
    n = len(scores)
    strict = sum(1 for s in scores if s["strict_pass"])
    recall_avg = sum(s["recall_R"] for s in scores) / n
    rj = [1.0 if s["strict_pass"] else 0.0 for s in scores]
    r_mean = sum(rj) / n
    r_std = (sum((x - r_mean) ** 2 for x in rj) / n) ** 0.5 if n > 1 else 0.0

    out: dict[str, Any] = {
        "runs": n,
        "strict_pass_count": strict,
        "strict_pass_rate": round(strict / n, 4),
        "recall_R_mean": round(recall_avg, 4),
        "r_mean": round(r_mean, 4),
        "r_std": round(r_std, 4),
        "r_min": min(rj),
        "r_max": max(rj),
    }
    for k in (1, 3, 5):
        if n >= k:
            out[f"pass@{k}"] = round(pass_at_k(strict, n, k), 4)
            out[f"pass^{k}"] = round(pass_all_k(strict, n, k), 4)
    return out


def format_report(
    task_id: str, per_run: list[dict[str, Any]], agg: dict[str, Any]
) -> str:
    lines = [
        f"# Eval review_agent — {task_id}",
        "",
        "## Por run",
    ]
    for i, s in enumerate(per_run):
        lines.append(
            f"- run {i}: StrictPass={s['strict_pass']} Recall@R={s['recall_R']} "
            f"veredito={s['veredito']} (esp. {s['veredito_esperado']}) "
            f"falsos_P1={s['falsos_p1_preexistente']} agentes={s['agentes_acionados']}"
        )
        if s["regressoes_esperadas"]:
            miss = set(s["regressoes_esperadas"]) - set(s["regressoes_detectadas"])
            if miss:
                lines.append(f"  - regressões não detectadas: {sorted(miss)}")
    lines.extend(["", "## Agregado", ""])
    for k, v in agg.items():
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines) + "\n"
