"""
Pipeline de Integração: migration_agent → test_agent → review_agent

Executa o fluxo completo usando Groq (ChatGroq) para todos os agentes.

Uso:
    python test_pipeline.py [--input url.py] [--output-dir .pipeline_output]

Pré-requisito:
    GROQ_API_KEY em variável de ambiente ou arquivo .env
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Configuração de paths ──────────────────────────────────────────────────────

REPO_ROOT        = Path(__file__).resolve().parent
MIGRATION_DIR    = REPO_ROOT / "migration_agent"
TEST_AGENT_DIR   = REPO_ROOT / "test_agent"
REVIEW_AGENT_DIR = REPO_ROOT / "review_agent"

for d in [str(REPO_ROOT), str(MIGRATION_DIR), str(TEST_AGENT_DIR), str(REVIEW_AGENT_DIR)]:
    if d not in sys.path:
        sys.path.insert(0, d)

# ── Patch ChatOllama → ChatGroq ───────────────────────────────────────────────

from dotenv import load_dotenv
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)

GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY")
    or os.getenv("GROQ_KEY")
    or os.getenv("API_KEY")
)
if not GROQ_API_KEY:
    print("ERRO: GROQ_API_KEY não encontrada.")
    sys.exit(2)

import types
import langchain_groq

def _make_groq_compat(model: str = "llama3", temperature: float = 0, **_kwargs):
    import traceback
    stack = "".join(traceback.format_stack())
    use_large = "test_agent" in stack or "agent.py" in stack
    model_name = "llama-3.3-70b-versatile" if use_large else "llama-3.1-8b-instant"
    return langchain_groq.ChatGroq(
        api_key=GROQ_API_KEY,
        model_name=model_name,
        temperature=temperature,
    )

_fake_ollama_mod = types.ModuleType("langchain_ollama")
_fake_ollama_mod.ChatOllama = _make_groq_compat  # type: ignore[attr-defined]
sys.modules.setdefault("langchain_ollama", _fake_ollama_mod)

# ── Imports dos agentes ────────────────────────────────────────────────────────

def _import_migration_agent():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "langgraph_mig03", MIGRATION_DIR / "langgraph-mig03.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_test_agent():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "test_agent_agent", TEST_AGENT_DIR / "agent" / "agent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_review_agent():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "review_agent", REVIEW_AGENT_DIR / "review-agent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Funções de chamada de cada agente ─────────────────────────────────────────

def run_migration(codigo_original: str, num_examples: int = 10) -> dict:
    print(f"\n{'='*60}")
    print("  ETAPA — MIGRATION AGENT")
    print(f"{'='*60}")
    t0 = time.time()

    mig = _import_migration_agent()
    exemplos = mig.carregar_exemplos_treino(num_examples) or []
    prompt_sistema = mig.criar_prompt_treino(exemplos)
    agente = mig.criar_agente_migracao(exemplos, prompt_sistema)

    from langchain_core.messages import HumanMessage, AIMessage

    resultado = agente.invoke({
        "messages":            [HumanMessage(content="Migrar código urllib para requests")],
        "codigo_usuario":      codigo_original,
        "codigo_migrado":      "",
        "inferencia_semantica": "",
        "analise_agente":      "",
        "status":              "",
    })

    elapsed        = time.time() - t0
    codigo_migrado = resultado.get("codigo_migrado", "")
    status         = resultado.get("status", "unknown")
    messages_text  = [m.content for m in resultado.get("messages", []) if isinstance(m, AIMessage)]

    print(f"  Status  : {status}")
    print(f"  Tempo   : {elapsed:.1f}s")
    print(f"  Linhas  : {len(codigo_migrado.splitlines())}")

    if not codigo_migrado.strip():
        print("  ERRO: Migration não gerou código.")
        sys.exit(3)

    return {
        "original_code":        codigo_original,
        "migrated_code":        codigo_migrado,
        "status":               status,
        "messages":             messages_text,
        "inferencia_semantica": resultado.get("inferencia_semantica", ""),
        "elapsed_s":            elapsed,
    }


def run_test(original_code: str, migrated_code: str) -> dict:
    """
    Executa o test_agent e retorna o resultado completo incluindo router_decision.
    """
    print(f"\n{'='*60}")
    print("  ETAPA — TEST AGENT")
    print(f"{'='*60}")
    t0 = time.time()

    ta     = _import_test_agent()
    result = ta.run_agent(original_code, migrated_code)
    elapsed = time.time() - t0

    evaluation      = result.get("evaluation", {})
    router_decision = result.get("router_decision", {})
    needs_revision  = router_decision.get("needs_revision", False)
    verdict         = "NEEDS_REVISION" if needs_revision else "APPROVED"

    print(f"  Router decision : {verdict}")
    print(f"  Equivalence     : {evaluation.get('execution_summary', {}).get('equivalence_rate', 'N/A')}%")
    print(f"  Valid baseline  : {evaluation.get('execution_summary', {}).get('valid_baseline', 'N/A')} tests")
    print(f"  Regressions     : {evaluation.get('execution_summary', {}).get('regressions', 'N/A')}")
    print(f"  Tempo           : {elapsed:.1f}s")

    for reason in router_decision.get("reasons", []):
        print(f"  → {reason}")
    for suggestion in router_decision.get("suggestions", []):
        print(f"  💡 {suggestion}")

    return {
        "report":          result.get("report", ""),
        "evaluation":      evaluation,
        "router_decision": router_decision,
        "needs_revision":  needs_revision,
        "elapsed_s":       elapsed,
    }


def run_review(original_code: str, migrated_code: str, max_retries: int = 3) -> dict:
    print(f"\n{'='*60}")
    print("  ETAPA — REVIEW AGENT")
    print(f"{'='*60}")
    t0 = time.time()

    ra = _import_review_agent()

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  Executando (tentativa {attempt}/{max_retries})...")
            result = ra._executar_grafo(original_code, migrated_code)
            break
        except Exception as exc:
            err_str = str(exc)
            is_rate_limit  = "rate_limit" in err_str.lower() or "429" in err_str
            is_daily_limit = "tokens per day" in err_str.lower() or "TPD" in err_str
            if is_rate_limit:
                if is_daily_limit:
                    print("  ATENÇÃO: Limite DIÁRIO de tokens Groq atingido.")
                    raise
                wait = 45 * attempt
                print(f"  Rate limit — aguardando {wait}s...")
                time.sleep(wait)
                if attempt == max_retries:
                    raise
            else:
                raise

    elapsed = time.time() - t0
    print(f"  Deve reprocessar : {result.get('deve_reprocessar', False)}")
    print(f"  Tempo            : {elapsed:.1f}s")

    return {**result, "elapsed_s": elapsed}


# ── Pipeline principal ─────────────────────────────────────────────────────────

MAX_REVISION_LOOPS = 3   # evita loop infinito

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Pipeline integrado: migration → test → review")
    parser.add_argument("--input",       default=str(REPO_ROOT / "url.py"))
    parser.add_argument("--output-dir",  default=str(REPO_ROOT / ".pipeline_output"))
    parser.add_argument("--examples",    type=int, default=10)
    parser.add_argument("--skip-test",   action="store_true")
    parser.add_argument("--skip-review", action="store_true")
    args = parser.parse_args()

    out_dir    = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    input_path = Path(args.input)

    if not input_path.exists():
        print(f"ERRO: Arquivo não encontrado: {input_path}")
        sys.exit(1)

    original_code = input_path.read_text(encoding="utf-8")

    print(f"\n{'#'*60}")
    print(f"  PIPELINE DE INTEGRAÇÃO")
    print(f"  Início : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Input  : {input_path}")
    print(f"  Output : {out_dir.resolve()}")
    print(f"{'#'*60}")

    pipeline_start  = time.time()
    migration_result = None
    test_result      = {"decision": "skipped"}
    review_result    = {}

    # ── Loop migration → test (com revisão se necessário) ─────────────────────
    current_original = original_code
    current_migrated = ""
    revision_loop    = 0

    while revision_loop <= MAX_REVISION_LOOPS:

        # ── ETAPA: Migration ──────────────────────────────────────────────────
        # Na primeira iteração migra do original.
        # Nas revisões subsequentes o migrador recebe o código original + contexto
        # das regressões detectadas pelo Router para guiar a remigração.
        if revision_loop == 0:
            migration_result = run_migration(current_original, num_examples=args.examples)
        else:
            # Passa as sugestões do Router como contexto adicional para o migrador
            suggestions = test_result.get("router_decision", {}).get("suggestions", [])
            context = (
                "\n\nCONTEXTO DE REVISÃO (iteração {}):\n".format(revision_loop)
                + "\n".join(f"- {s}" for s in suggestions)
            ) if suggestions else ""
            migration_result = run_migration(current_original + context, num_examples=args.examples)

        current_migrated = migration_result["migrated_code"]

        # Salva artefato da iteração
        suffix = f"_iter{revision_loop}" if revision_loop > 0 else ""
        (out_dir / f"migrated_code{suffix}.py").write_text(current_migrated, encoding="utf-8")
        (out_dir / f"migration_result{suffix}.json").write_text(
            json.dumps({k: v for k, v in migration_result.items() if k != "migrated_code"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # ── ETAPA: Test ───────────────────────────────────────────────────────
        if args.skip_test:
            print("\n  ETAPA TEST AGENT: pulada (--skip-test)")
            test_result = {"decision": "skipped", "needs_revision": False}
            break

        try:
            test_result = run_test(current_original, current_migrated)
        except Exception as exc:
            print(f"\n  AVISO: test_agent falhou: {exc}")
            test_result = {"error": str(exc), "needs_revision": False}
            break

        # Salva relatório de teste da iteração
        if test_result.get("report"):
            (out_dir / f"test_report{suffix}.md").write_text(
                test_result["report"], encoding="utf-8"
            )
        (out_dir / f"test_result{suffix}.json").write_text(
            json.dumps({k: v for k, v in test_result.items() if k != "report"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # ── Decisão do Router ─────────────────────────────────────────────────
        needs_revision = test_result.get("needs_revision", False)

        if not needs_revision:
            print(f"\n  ✅ Router: APPROVED após {revision_loop + 1} iteração(ões)")
            break

        revision_loop += 1

        if revision_loop > MAX_REVISION_LOOPS:
            print(f"\n  ⚠️  Limite de {MAX_REVISION_LOOPS} revisões atingido — prosseguindo para review")
            break

        print(f"\n  🔄 Router: NEEDS_REVISION — iniciando iteração {revision_loop}/{MAX_REVISION_LOOPS}")
        print(f"  Aguardando 15s para respeitar rate limit do Groq...")
        time.sleep(15)

    # ── ETAPA: Review ─────────────────────────────────────────────────────────
    if not args.skip_review:
        print("\n  Aguardando 15s antes do review_agent...")
        time.sleep(15)
        try:
            review_result = run_review(current_original, current_migrated)
        except Exception as exc:
            print(f"\n  AVISO: review_agent falhou: {exc}")
            review_result = {"error": str(exc)}
    else:
        print("\n  ETAPA REVIEW AGENT: pulada (--skip-review)")

    if review_result.get("relatorio_final"):
        (out_dir / "review_report.md").write_text(
            review_result["relatorio_final"], encoding="utf-8"
        )
    if review_result:
        (out_dir / "review_result.json").write_text(
            json.dumps({k: v for k, v in review_result.items() if k != "relatorio_final"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Sumário final ─────────────────────────────────────────────────────────
    total_elapsed = time.time() - pipeline_start

    pipeline_summary = {
        "pipeline_start":   datetime.now().isoformat(),
        "total_elapsed_s":  round(total_elapsed, 1),
        "input_file":       str(input_path),
        "revision_loops":   revision_loop,
        "stages": {
            "migration": {
                "status":          migration_result.get("status") if migration_result else None,
                "elapsed_s":       migration_result.get("elapsed_s") if migration_result else None,
                "migrated_lines":  len(current_migrated.splitlines()),
            },
            "test": {
                "needs_revision":  test_result.get("needs_revision"),
                "equivalence":     test_result.get("evaluation", {}).get("execution_summary", {}).get("equivalence_rate"),
                "regressions":     test_result.get("evaluation", {}).get("execution_summary", {}).get("regressions"),
                "elapsed_s":       test_result.get("elapsed_s"),
                "error":           test_result.get("error"),
            },
            "review": {
                "deve_reprocessar": review_result.get("deve_reprocessar"),
                "achados_total": (
                    len(review_result.get("achados_semantica") or [])
                    + len(review_result.get("achados_seguranca") or [])
                    + len(review_result.get("achados_lint") or [])
                ),
                "elapsed_s": review_result.get("elapsed_s"),
                "error":     review_result.get("error"),
            },
        },
    }

    (out_dir / "pipeline_summary.json").write_text(
        json.dumps(pipeline_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{'#'*60}")
    print(f"  PIPELINE CONCLUÍDO")
    print(f"  Tempo total     : {total_elapsed:.1f}s")
    print(f"  Iterações loop  : {revision_loop}")
    print(f"  Migration       : {migration_result.get('status') if migration_result else 'N/A'}")
    print(f"  Test            : {'NEEDS_REVISION' if test_result.get('needs_revision') else test_result.get('decision', 'skipped')}")
    if review_result.get("error"):
        print(f"  Review          : erro — {review_result['error'][:80]}")
    elif args.skip_review:
        print(f"  Review          : pulado")
    else:
        print(f"  Review          : {'reprocessar' if review_result.get('deve_reprocessar') else 'aprovado'}")
    print(f"  Artefatos       : {out_dir.resolve()}")
    print(f"{'#'*60}\n")

    relatorio = review_result.get("relatorio_final", "")
    if relatorio:
        preview = relatorio[:1200]
        print("── PRÉVIA DO RELATÓRIO DE REVISÃO ──────────────────────")
        print(preview)
        if len(relatorio) > 1200:
            print(f"... [+{len(relatorio)-1200} chars — ver {out_dir}/review_report.md]")
        print("────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()