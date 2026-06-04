#!/usr/bin/env python3
"""Loop de auto-correcao entre migration_agent e review_agent."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_AGENT_PATH = REPO_ROOT / "migration_agent" / "langgraph-mig03.py"
REVIEW_AGENT_PATH = REPO_ROOT / "review_agent" / "review-agent.py"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".loop_output"
MAX_ITERACOES_PADRAO = 3


def _carregar_modulo(nome: str, caminho: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(nome, caminho)
    if spec is None or spec.loader is None:
        raise ImportError(f"Nao foi possivel carregar o modulo {nome} em {caminho}")

    mod = importlib.util.module_from_spec(spec)
    sys.modules[nome] = mod
    spec.loader.exec_module(mod)
    return mod


def _contar_bloqueadores(achados: list[dict[str, Any]]) -> tuple[int, int]:
    p0 = sum(
        1
        for achado in achados
        if achado.get("prefixo") and achado.get("severidade") == "P0"
    )
    p1 = sum(
        1
        for achado in achados
        if achado.get("prefixo") and achado.get("severidade") == "P1"
    )
    return p0, p1


def _contar_por_severidade(achados: list[dict[str, Any]]) -> dict[str, int]:
    return {
        sev: sum(
            1
            for achado in achados
            if achado.get("prefixo") and achado.get("severidade") == sev
        )
        for sev in ("P0", "P1", "P2", "P3")
    }


def _funcoes_por_linha(codigo: str) -> dict[int, str]:
    try:
        tree = ast.parse(codigo)
    except SyntaxError:
        return {}

    indice: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_lineno = getattr(node, "end_lineno", node.lineno)
            for linha in range(node.lineno, end_lineno + 1):
                indice[linha] = node.name
    return indice


def _trecho_funcao(codigo: str, simbolo: str | None) -> str:
    if not simbolo:
        return ""

    try:
        tree = ast.parse(codigo)
    except SyntaxError:
        return ""

    linhas = codigo.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == simbolo:
            end_lineno = getattr(node, "end_lineno", node.lineno)
            return "\n".join(linhas[node.lineno - 1:end_lineno])
    return ""


def _categoria_achado(achado: dict[str, Any]) -> str:
    texto = " ".join(
        str(achado.get(chave, ""))
        for chave in ("raw", "descricao", "trigger", "simbolo")
    )
    if re.search(r"HTTP|status|4xx|5xx|raise_for_status|urlopen|requests\.get", texto, re.I):
        return "HTTP_ERROR_HANDLING"
    if re.search(r"json|decode|text|content|bytes|encoding", texto, re.I):
        return "RESPONSE_PARSING"
    if re.search(r"header|cookie|auth|token|credential", texto, re.I):
        return "REQUEST_CONTRACT"
    if re.search(r"exception|except|error handling|timeout", texto, re.I):
        return "EXCEPTION_SEMANTICS"
    return "MIGRATION_SEMANTICS"


def _nome_chamada(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _nome_chamada(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _nome_chamada(node.func)
    return ""


def _fonte_no(linhas: list[str], node: ast.AST) -> str:
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", None)
    if not isinstance(lineno, int) or not isinstance(end_lineno, int):
        return ""
    return "\n".join(linhas[lineno - 1:end_lineno]).strip()


def _resumo_semantico_funcao(trecho: str) -> dict[str, Any]:
    if not trecho.strip():
        return {
            "has_code": False,
            "http_operations": [],
            "status_validation": [],
            "has_try_except": False,
            "exception_handlers": [],
            "observable_calls": [],
            "returns_value": False,
            "raises_explicitly": False,
        }

    try:
        tree = ast.parse(trecho)
    except SyntaxError:
        return {
            "has_code": True,
            "parse_error": True,
            "raw": trecho,
        }

    linhas = trecho.splitlines()
    calls: list[str] = []
    http_operations: list[str] = []
    status_validation: list[str] = []
    exception_handlers: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            nome = _nome_chamada(node.func)
            if nome:
                calls.append(nome)
            if re.search(r"(urlopen|requests\.(get|post|put|delete|patch|request))$", nome):
                http_operations.append(_fonte_no(linhas, node) or nome)
            if nome.endswith("raise_for_status"):
                status_validation.append(_fonte_no(linhas, node) or nome)

        if isinstance(node, ast.Try):
            for handler in node.handlers:
                tipo = _nome_chamada(handler.type) if handler.type else "BaseException"
                corpo = [
                    _fonte_no(linhas, stmt)
                    for stmt in handler.body
                    if _fonte_no(linhas, stmt)
                ]
                exception_handlers.append({"type": tipo, "body": corpo})

    observable_prefixes = ("info", "warn", "logging", "print", "raise")
    observable_calls = sorted(
        {
            call
            for call in calls
            if call.split(".")[0] in observable_prefixes
        }
    )

    return {
        "has_code": True,
        "http_operations": http_operations,
        "status_validation": status_validation,
        "has_try_except": bool(exception_handlers),
        "exception_handlers": exception_handlers,
        "observable_calls": observable_calls,
        "returns_value": any(isinstance(node, ast.Return) and node.value for node in ast.walk(tree)),
        "raises_explicitly": any(isinstance(node, ast.Raise) for node in ast.walk(tree)),
    }


def _deltas_semanticos(original: dict[str, Any], migrado: dict[str, Any]) -> list[str]:
    deltas: list[str] = []

    if original.get("has_try_except") and not migrado.get("has_try_except"):
        deltas.append("lost_exception_handling")
    if original.get("exception_handlers") != migrado.get("exception_handlers"):
        deltas.append("exception_observable_effects_changed")
    if bool(original.get("status_validation")) != bool(migrado.get("status_validation")):
        deltas.append("status_validation_changed")
    if set(original.get("observable_calls", [])) != set(migrado.get("observable_calls", [])):
        deltas.append("observable_side_effects_changed")
    if bool(original.get("returns_value")) != bool(migrado.get("returns_value")):
        deltas.append("return_contract_changed")
    if bool(original.get("raises_explicitly")) != bool(migrado.get("raises_explicitly")):
        deltas.append("explicit_raise_contract_changed")

    return deltas


def _invariante_por_categoria(categoria: str) -> str:
    invariantes = {
        "HTTP_ERROR_HANDLING": (
            "HTTP operations in the migrated code must preserve the original "
            "observable success and failure behavior."
        ),
        "EXCEPTION_SEMANTICS": (
            "Exception handling in the migrated code must preserve the original "
            "observable error contract."
        ),
        "RESPONSE_PARSING": (
            "Response decoding/parsing in the migrated code must preserve the "
            "original data format and value semantics."
        ),
        "REQUEST_CONTRACT": (
            "The migrated request must preserve the original method, headers, "
            "payload, authentication, and cookie semantics."
        ),
        "MIGRATION_SEMANTICS": (
            "The migrated code must preserve the original observable behavior for "
            "the affected symbol."
        ),
    }
    return invariantes.get(categoria, invariantes["MIGRATION_SEMANTICS"])


def _simbolo_acionavel(
    achado: dict[str, Any],
    codigo_migrado: str,
    fallback_symbols: list[str],
    index: int,
) -> str | None:
    simbolo = achado.get("simbolo")
    if isinstance(simbolo, str) and re.match(r"^[A-Za-z_]\w*$", simbolo):
        return simbolo

    linha = achado.get("linha") or achado.get("linha_llm")
    if isinstance(linha, int):
        por_linha = _funcoes_por_linha(codigo_migrado)
        if linha in por_linha:
            return por_linha[linha]

    if len(fallback_symbols) == 1:
        return fallback_symbols[0]
    if index < len(fallback_symbols):
        return fallback_symbols[index]
    return None


def _compilar_achado_para_feedback(
    achado: dict[str, Any],
    *,
    original: str,
    migrado: str,
    fallback_symbols: list[str],
    index: int,
) -> dict[str, Any]:
    categoria = _categoria_achado(achado)
    simbolo = _simbolo_acionavel(achado, migrado, fallback_symbols, index)
    trecho_original = _trecho_funcao(original, simbolo)
    trecho_migrado = _trecho_funcao(migrado, simbolo)
    original_semantics = _resumo_semantico_funcao(trecho_original)
    migrated_semantics = _resumo_semantico_funcao(trecho_migrado)
    semantic_deltas = _deltas_semanticos(original_semantics, migrated_semantics)
    invariant = _invariante_por_categoria(categoria)

    return {
        "severity": achado.get("severidade", "P1"),
        "category": categoria,
        "confidence": 0.9,
        "symbol": simbolo,
        "problem": achado.get("descricao") or achado.get("raw", ""),
        "trigger": achado.get("trigger", ""),
        "semantic_constraint": {
            "invariant": invariant,
            "must_preserve": original_semantics,
            "current_migrated_semantics": migrated_semantics,
            "semantic_deltas": semantic_deltas,
            "repair_objective": (
                "Change the migrated code so current_migrated_semantics satisfies "
                "must_preserve for this category. Prefer the smallest local edit and "
                "do not alter unrelated behavior."
            ),
        },
        "expected_action": "Satisfy the semantic_constraint for this finding.",
        "repair_hint": (
            "Use the original and migrated snippets as evidence. Restore the original "
            "observable contract instead of applying a canned fix."
        ),
        "original_snippet": trecho_original,
        "migrated_snippet": trecho_migrado,
        "raw_finding": achado.get("formatado") or achado.get("raw") or str(achado),
    }


def montar_feedback(review_out: dict[str, Any], original: str, migrado: str) -> str:
    relatorio = (review_out.get("relatorio_final") or "").strip()
    achados = review_out.get("achados_estruturados") or []
    bloqueadores = [
        achado
        for achado in achados
        if achado.get("prefixo") and achado.get("severidade") in {"P0", "P1"}
    ]
    fallback_symbols = (
        review_out.get("diff", {}).get("altered_functions", [])
        if isinstance(review_out.get("diff"), dict)
        else []
    )
    structured = [
        _compilar_achado_para_feedback(
            achado,
            original=original,
            migrado=migrado,
            fallback_symbols=fallback_symbols,
            index=index,
        )
        for index, achado in enumerate(bloqueadores)
    ]

    payload = {"blocking_findings": structured}

    partes = [
        "The review agent found blocking issues in the urllib-to-requests migration.",
        "Use the structured findings below as the source of truth.",
        "Fix ONLY these P0/P1 issues and preserve all unrelated behavior.",
        "",
        "## Machine-readable feedback (JSON)",
        json.dumps(payload, ensure_ascii=False, indent=2),
        "",
        "## Repair objectives",
    ]
    for index, item in enumerate(structured, start=1):
        partes.extend([
            f"[{index}] {item['category']} / {item['severity']} / {item.get('symbol') or 'unknown symbol'}",
            f"Observed issue: {item['problem']}",
            f"Semantic invariant: {item['semantic_constraint']['invariant']}",
            "Semantic deltas: "
            + ", ".join(item["semantic_constraint"]["semantic_deltas"] or ["unspecified"]),
            f"Repair objective: {item['semantic_constraint']['repair_objective']}",
            f"Suggested repair: {item['repair_hint']}",
            "",
        ])

    if relatorio:
        partes.extend([
            "## Human report reference",
            "Use this only as supporting context; prefer the structured JSON above.",
            relatorio,
        ])

    partes.append("")
    partes.append("Return ONLY the corrected Python code without explanation or markdown.")
    return "\n".join(partes)


def executar_loop(
    original: str,
    *,
    max_iteracoes: int = MAX_ITERACOES_PADRAO,
    exemplos: int = 10,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    mig = _carregar_modulo("langgraph_mig03", MIGRATION_AGENT_PATH)
    rev = _carregar_modulo("review_agent_loop", REVIEW_AGENT_PATH)

    exemplos_treino, prompt_sistema = mig.preparar_contexto_migracao(exemplos)

    inicio = time.time()
    historico: list[dict[str, Any]] = []
    print("[loop] Migracao inicial...")
    migrado = mig.migrar_codigo(original, exemplos_treino, prompt_sistema)
    if not migrado.strip():
        raise RuntimeError("migration_agent nao gerou codigo migrado")

    status = "limite_atingido"
    review_out: dict[str, Any] = {}

    for iteracao in range(1, max_iteracoes + 1):
        print(f"[loop] Review {iteracao}/{max_iteracoes}...")
        review_out = rev._executar_grafo(original, migrado)

        achados = review_out.get("achados_estruturados") or []
        contagens = _contar_por_severidade(achados)
        p0, p1 = _contar_bloqueadores(achados)

        historico.append(
            {
                "iteracao": iteracao,
                "contagens": contagens,
                "deve_reprocessar": review_out.get("deve_reprocessar"),
                "veredito": review_out.get("veredito"),
            }
        )

        (output_dir / f"codigo_migrado_iter{iteracao}.py").write_text(
            migrado.strip() + "\n",
            encoding="utf-8",
        )
        if review_out.get("relatorio_final"):
            (output_dir / f"relatorio_iter{iteracao}.md").write_text(
                review_out["relatorio_final"],
                encoding="utf-8",
            )

        print(
            "[loop] Achados: "
            f"P0={contagens['P0']} P1={contagens['P1']} "
            f"P2={contagens['P2']} P3={contagens['P3']}"
        )

        if p0 + p1 == 0:
            status = "aprovado"
            break

        if iteracao == max_iteracoes:
            status = "limite_atingido"
            break

        feedback = montar_feedback(review_out, original, migrado)
        (output_dir / f"feedback_iter{iteracao}.txt").write_text(
            feedback,
            encoding="utf-8",
        )
        print("[loop] Refinando migracao com feedback do review...")
        migrado_refinado = mig.aplicar_feedback_revisao(
            original,
            migrado,
            feedback,
            exemplos_treino,
            prompt_sistema,
        )
        if not migrado_refinado.strip():
            raise RuntimeError("migration_agent nao gerou codigo ao aplicar feedback")
        migrado = migrado_refinado

    resultado = {
        "status": status,
        "iteracoes": len(historico),
        "elapsed_s": round(time.time() - inicio, 2),
        "historico": historico,
        "codigo_migrado_final": migrado,
        "relatorio_final": review_out.get("relatorio_final", ""),
        "review_final": {
            key: value
            for key, value in review_out.items()
            if key != "relatorio_final"
        },
    }

    (output_dir / "codigo_migrado_final.py").write_text(
        migrado.strip() + "\n",
        encoding="utf-8",
    )
    (output_dir / "relatorio_final.md").write_text(
        resultado["relatorio_final"],
        encoding="utf-8",
    )
    (output_dir / "loop_resultado.json").write_text(
        json.dumps(
            {k: v for k, v in resultado.items() if k not in {"codigo_migrado_final", "relatorio_final"}},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return resultado


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Executa o loop migration_agent -> review_agent -> migration_agent."
    )
    parser.add_argument(
        "--input",
        default=str(REPO_ROOT / "url.py"),
        help="Arquivo Python urllib de entrada (padrao: url.py).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Diretorio dos artefatos do loop (padrao: .loop_output).",
    )
    parser.add_argument(
        "--max-iteracoes",
        type=int,
        default=MAX_ITERACOES_PADRAO,
        help="Limite de rodadas de review (padrao: 3).",
    )
    parser.add_argument(
        "--exemplos",
        type=int,
        default=10,
        help="Quantidade de exemplos few-shot usados pelo migration_agent.",
    )
    args = parser.parse_args()

    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERRO: arquivo de entrada nao encontrado: {input_path}", file=sys.stderr)
        return 2

    print(f"[loop] Inicio: {datetime.now().isoformat(timespec='seconds')}")
    print(f"[loop] Input : {input_path}")
    print(f"[loop] Output: {Path(args.output_dir).resolve()}")

    try:
        resultado = executar_loop(
            input_path.read_text(encoding="utf-8"),
            max_iteracoes=args.max_iteracoes,
            exemplos=args.exemplos,
            output_dir=Path(args.output_dir),
        )
    except Exception as exc:
        print(f"ERRO: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"[loop] Status final: {resultado['status']}")
    print(f"[loop] Iteracoes   : {resultado['iteracoes']}")
    return 0 if resultado["status"] == "aprovado" else 3


if __name__ == "__main__":
    raise SystemExit(main())
