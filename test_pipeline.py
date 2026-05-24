"""
Pipeline de Integração: migration_agent → test_agent → review_agent

Executa o fluxo completo usando Groq (ChatGroq) para todos os agentes,
já que Ollama não está disponível neste ambiente.

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

REPO_ROOT = Path(__file__).resolve().parent
MIGRATION_DIR = REPO_ROOT / "migration_agent"
TEST_AGENT_DIR = REPO_ROOT / "test_agent"
REVIEW_AGENT_DIR = REPO_ROOT / "review_agent"

# Adiciona todos os diretórios ao sys.path antes de qualquer import de agente
for d in [str(REPO_ROOT), str(MIGRATION_DIR), str(TEST_AGENT_DIR), str(REVIEW_AGENT_DIR)]:
    if d not in sys.path:
        sys.path.insert(0, d)

# ── Patch ChatOllama → ChatGroq ANTES de importar os agentes ──────────────────
# migration_agent e test_agent foram originalmente escritos com ChatOllama.
# Como Ollama não está instalado neste ambiente, substituímos por ChatGroq.

from dotenv import load_dotenv
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)

GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY")
    or os.getenv("GROQ_KEY")
    or os.getenv("API_KEY")
)
if not GROQ_API_KEY:
    print("ERRO: GROQ_API_KEY não encontrada. Defina a variável de ambiente ou crie .env")
    sys.exit(2)

# Monkey-patch: cria módulo langchain_ollama falso que delega para ChatGroq
import types
import langchain_groq

def _make_groq_compat(model: str = "llama3", temperature: float = 0, **_kwargs):
    """
    Substitui ChatOllama por ChatGroq.
    Usa llama-3.1-8b-instant para o migration_agent (inferência + migração = prompts pequenos).
    Usa llama-3.3-70b-versatile para o test_agent (node_analyzer envia código completo = prompts grandes).
    Detectado no import via tamanho do contexto necessário.
    """
    # test_agent/agent/agent.py chama com model="llama3" mas precisa de janela maior
    # O patch usa 70B para garantir que o analyzer (8K tokens) caiba no TPM
    # e 8B apenas para migration (prompts menores, mais RPM)
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

# ── Imports dos agentes (após o patch) ────────────────────────────────────────

def _import_migration_agent():
    """Importa o migration_agent sem executar o bloco __main__."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "langgraph_mig03",
        MIGRATION_DIR / "langgraph-mig03.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Sinaliza para o módulo não gravar arquivos (WRITE_ARTIFACTS = False)
    mod.__name__ = "langgraph_mig03"
    spec.loader.exec_module(mod)
    return mod


def _import_test_agent():
    """Importa o test_agent."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "test_agent_agent",
        TEST_AGENT_DIR / "agent" / "agent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_review_agent():
    """Importa o review_agent (FastAPI + grafo LangGraph)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "review_agent",
        REVIEW_AGENT_DIR / "review-agent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Funções de chamada de cada agente ─────────────────────────────────────────

def run_migration(codigo_original: str, num_examples: int = 10) -> dict:
    """
    Executa o migration_agent.
    Retorna dict com: codigo_migrado, status, messages, inferencia_semantica
    """
    print(f"\n{'='*60}")
    print("  ETAPA 1 — MIGRATION AGENT")
    print(f"{'='*60}")
    t0 = time.time()

    mig = _import_migration_agent()

    print(f"  Carregando {num_examples} exemplos do dataset...")
    exemplos = mig.carregar_exemplos_treino(num_examples)
    if not exemplos:
        print("  AVISO: Nenhum exemplo carregado — usando prompt base sem few-shot.")
        exemplos = []

    prompt_sistema = mig.criar_prompt_treino(exemplos)
    agente = mig.criar_agente_migracao(exemplos, prompt_sistema)

    from langchain_core.messages import HumanMessage, AIMessage

    resultado = agente.invoke({
        "messages": [HumanMessage(content="Migrar código urllib para requests")],
        "codigo_usuario": codigo_original,
        "codigo_migrado": "",
        "inferencia_semantica": "",
        "analise_agente": "",
        "status": "",
    })

    elapsed = time.time() - t0
    codigo_migrado = resultado.get("codigo_migrado", "")
    status = resultado.get("status", "unknown")

    messages_text = [
        m.content for m in resultado.get("messages", [])
        if isinstance(m, AIMessage)
    ]

    print(f"  Status  : {status}")
    print(f"  Tempo   : {elapsed:.1f}s")
    print(f"  Linhas migradas: {len(codigo_migrado.splitlines())}")
    for msg in messages_text:
        print(f"  > {msg}")

    if not codigo_migrado.strip():
        print("  ERRO: Migration não gerou código.")
        sys.exit(3)

    return {
        "original_code": codigo_original,
        "migrated_code": codigo_migrado,
        "status": status,
        "messages": messages_text,
        "inferencia_semantica": resultado.get("inferencia_semantica", ""),
        "elapsed_s": elapsed,
    }


def run_test(original_code: str, migrated_code: str) -> dict:
    """
    Executa o test_agent.
    Retorna dict com: report, evaluation, decision, iteration
    """
    print(f"\n{'='*60}")
    print("  ETAPA 2 — TEST AGENT")
    print(f"{'='*60}")
    t0 = time.time()

    ta = _import_test_agent()

    print("  Construindo grafo de testes...")
    result = ta.run_agent(original_code, migrated_code)
    elapsed = time.time() - t0

    evaluation = result.get("evaluation", {})
    scores = evaluation.get("scores", {}) if isinstance(evaluation, dict) else {}
    decision = result.get("decision", "unknown")

    print(f"  Decision  : {decision}")
    print(f"  Iterações : {result.get('iteration', 0)}")
    print(f"  Score     : {scores.get('overall', 'N/A')}")
    print(f"  Coverage  : orig={evaluation.get('coverage', {}).get('original', 'N/A')}%  mig={evaluation.get('coverage', {}).get('migrated', 'N/A')}%")
    print(f"  Tempo     : {elapsed:.1f}s")

    return {
        "report": result.get("report", ""),
        "evaluation": evaluation,
        "decision": decision,
        "iteration": result.get("iteration", 0),
        "elapsed_s": elapsed,
    }


def run_review(original_code: str, migrated_code: str, max_retries: int = 3) -> dict:
    """
    Executa o review_agent diretamente (sem FastAPI).
    Retorna dict com: relatorio_final, diff, achados_*, deve_reprocessar
    Tenta novamente em caso de rate limit (429) com backoff crescente.
    """
    print(f"\n{'='*60}")
    print("  ETAPA 3 — REVIEW AGENT")
    print(f"{'='*60}")
    t0 = time.time()

    ra = _import_review_agent()

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  Executando grafo de revisão (tentativa {attempt}/{max_retries})...")
            result = ra._executar_grafo(original_code, migrated_code)
            break
        except Exception as exc:
            err_str = str(exc)
            is_rate_limit = "rate_limit" in err_str.lower() or "429" in err_str
            is_daily_limit = "tokens per day" in err_str.lower() or "TPD" in err_str
            if is_rate_limit:
                if is_daily_limit:
                    # Limite diário — não adianta tentar novamente em minutos
                    print(f"\n  ATENÇÃO: Limite DIÁRIO de tokens Groq atingido.")
                    print(f"  Aguarde a meia-noite (horário UTC) para renovação da cota.")
                    print(f"  Use --skip-review ou execute novamente após a renovação.")
                    raise
                wait = 45 * attempt
                print(f"  Rate limit Groq (por minuto) — aguardando {wait}s...")
                time.sleep(wait)
                if attempt == max_retries:
                    raise
            else:
                raise

    elapsed = time.time() - t0

    print(f"  Agentes acionados : {result.get('agentes_acionados', [])}")
    print(f"  Iterações crítico : {result.get('iteracoes', 0)}")
    print(f"  Deve reprocessar  : {result.get('deve_reprocessar', False)}")
    print(f"  Achados semântica : {len(result.get('achados_semantica', []))}")
    print(f"  Achados segurança : {len(result.get('achados_seguranca', []))}")
    print(f"  Achados lint      : {len(result.get('achados_lint', []))}")
    print(f"  Tempo             : {elapsed:.1f}s")

    return {**result, "elapsed_s": elapsed}


# ── Pipeline principal ─────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Pipeline integrado: migration → test → review")
    parser.add_argument(
        "--input", default=str(REPO_ROOT / "url.py"),
        help="Caminho para o arquivo Python urllib de entrada (padrão: url.py)",
    )
    parser.add_argument(
        "--output-dir", default=str(REPO_ROOT / ".pipeline_output"),
        help="Diretório para salvar os artefatos de saída",
    )
    parser.add_argument(
        "--examples", type=int, default=10,
        help="Número de exemplos few-shot para o migration_agent (padrão: 10)",
    )
    parser.add_argument(
        "--skip-test", action="store_true",
        help="Pular etapa test_agent (útil se pytest-cov não estiver instalado)",
    )
    parser.add_argument(
        "--skip-review", action="store_true",
        help="Pular etapa review_agent (útil quando cota diária Groq está esgotada)",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERRO: Arquivo de entrada não encontrado: {input_path}")
        sys.exit(1)

    original_code = input_path.read_text(encoding="utf-8")

    print(f"\n{'#'*60}")
    print(f"  PIPELINE DE INTEGRAÇÃO")
    print(f"  Início: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Input : {input_path}")
    print(f"  Output: {out_dir.resolve()}")
    print(f"{'#'*60}")

    pipeline_start = time.time()

    # ── ETAPA 1: Migration ────────────────────────────────────────────────────
    migration_result = run_migration(original_code, num_examples=args.examples)
    migrated_code = migration_result["migrated_code"]

    (out_dir / "migrated_code.py").write_text(migrated_code, encoding="utf-8")
    (out_dir / "migration_result.json").write_text(
        json.dumps({k: v for k, v in migration_result.items() if k != "migrated_code"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── ETAPA 2: Test ─────────────────────────────────────────────────────────
    test_result: dict = {}
    if not args.skip_test:
        try:
            test_result = run_test(original_code, migrated_code)
            if test_result.get("report"):
                (out_dir / "test_report.md").write_text(
                    test_result["report"], encoding="utf-8"
                )
            (out_dir / "test_result.json").write_text(
                json.dumps({k: v for k, v in test_result.items() if k != "report"},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"\n  AVISO: test_agent falhou com erro: {exc}")
            print("  Continuando para o review_agent...")
            test_result = {"error": str(exc), "decision": "error"}
    else:
        print("\n  ETAPA 2 — TEST AGENT: pulada (--skip-test)")
        test_result = {"decision": "skipped"}

    # ── ETAPA 3: Review ───────────────────────────────────────────────────────
    review_result: dict = {}
    if not args.skip_review:
        # Aguarda um momento para evitar rate limit compartilhado com as etapas anteriores
        print("\n  Aguardando 15s para respeitar rate limit do Groq entre etapas...")
        time.sleep(15)
        try:
            review_result = run_review(original_code, migrated_code)
        except Exception as exc:
            print(f"\n  AVISO: review_agent falhou: {exc}")
            review_result = {"error": str(exc)}
    else:
        print("\n  ETAPA 3 — REVIEW AGENT: pulada (--skip-review)")
    if review_result.get("relatorio_final"):
        (out_dir / "review_report.md").write_text(
            review_result["relatorio_final"], encoding="utf-8"
        )

    if review_result:
        review_save = {k: v for k, v in review_result.items() if k != "relatorio_final"}
        (out_dir / "review_result.json").write_text(
            json.dumps(review_save, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Relatório final do pipeline ───────────────────────────────────────────
    total_elapsed = time.time() - pipeline_start

    pipeline_summary = {
        "pipeline_start": datetime.now().isoformat(),
        "total_elapsed_s": round(total_elapsed, 1),
        "input_file": str(input_path),
        "stages": {
            "migration": {
                "status": migration_result.get("status"),
                "elapsed_s": migration_result.get("elapsed_s"),
                "migrated_lines": len(migrated_code.splitlines()),
            },
            "test": {
                "decision": test_result.get("decision"),
                "iteration": test_result.get("iteration"),
                "elapsed_s": test_result.get("elapsed_s"),
                "error": test_result.get("error"),
            },
            "review": {
                "agentes_acionados": review_result.get("agentes_acionados"),
                "iteracoes": review_result.get("iteracoes"),
                "deve_reprocessar": review_result.get("deve_reprocessar"),
                "achados_total": (
                    len(review_result.get("achados_semantica") or [])
                    + len(review_result.get("achados_seguranca") or [])
                    + len(review_result.get("achados_lint") or [])
                ),
                "elapsed_s": review_result.get("elapsed_s"),
                "error": review_result.get("error"),
            },
        },
    }

    (out_dir / "pipeline_summary.json").write_text(
        json.dumps(pipeline_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{'#'*60}")
    print(f"  PIPELINE CONCLUÍDO")
    print(f"  Tempo total : {total_elapsed:.1f}s")
    print(f"  Migration   : {migration_result.get('status')}")
    print(f"  Test        : {test_result.get('decision', 'skipped')}")
    if review_result.get("error"):
        print(f"  Review      : erro — {review_result['error'][:80]}")
    elif args.skip_review:
        print(f"  Review      : pulado")
    else:
        print(f"  Review      : {'reprocessar' if review_result.get('deve_reprocessar') else 'aprovado'}")
    print(f"  Artefatos   : {out_dir.resolve()}")
    print(f"{'#'*60}\n")

    # Exibir prévia do relatório de revisão
    relatorio = review_result.get("relatorio_final", "") if review_result else ""
    if relatorio:
        preview = relatorio[:1200]
        print("── PRÉVIA DO RELATÓRIO DE REVISÃO ──────────────────────")
        print(preview)
        if len(relatorio) > 1200:
            print(f"... [+{len(relatorio)-1200} caracteres — ver {out_dir}/review_report.md]")
        print("────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
