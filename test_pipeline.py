"""
Pipeline de Integração: migration_agent → test_agent → review_agent

Detecta automaticamente se o Ollama está disponível e o usa como backend
do migration_agent (sem custo, sem rate limit). Cai para Groq se Ollama
não estiver rodando.

O review_agent usa Groq em todos os nós via API_3 (llama-3.3-70b-versatile).
O test_agent usa PROVIDER_API_KEY + PROVIDER_BASE_URL (API OpenAI-compatível).

Uso:
    python test_pipeline.py [--input url.py] [--output-dir .pipeline_output]

Variáveis de ambiente:
    API_3            — obrigatória para o review_agent (chave Groq)
    GROQ_API_KEY     — obrigatória se Ollama não estiver disponível (migration)
    PROVIDER_API_KEY — obrigatória para o test_agent
    PROVIDER_BASE_URL — URL base do test_agent (ex.: Groq OpenAI endpoint)
    OLLAMA_MODEL     — modelo Ollama (padrão: detectado automaticamente)
    OLLAMA_HOST      — host do Ollama (padrão: http://localhost:11434)
"""

from __future__ import annotations

import json
import os
import sys
import time
import types
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT       = Path(__file__).resolve().parent
MIGRATION_DIR   = REPO_ROOT / "migration_agent"
TEST_AGENT_DIR  = REPO_ROOT / "test_agent"
REVIEW_AGENT_DIR = REPO_ROOT / "review_agent"

for d in [str(REPO_ROOT), str(MIGRATION_DIR), str(TEST_AGENT_DIR), str(REVIEW_AGENT_DIR)]:
    if d not in sys.path:
        sys.path.insert(0, d)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)

# ── Detecção de Ollama ────────────────────────────────────────────────────────

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Ordem de preferência de modelos Ollama para migration + test
_OLLAMA_MODEL_PREFS = [
    "qwen2.5:14b", "qwen2.5-coder:14b",
    "llama3.1", "llama3.1:latest",
    "llama3.2", "llama3.2:latest",
    "llama3",   "llama3:latest",
    "llama3.3", "llama3.3:latest",
]

# Modelos preferidos para o review_agent (semântico → heavy)
_REVIEW_OLLAMA_HEAVY_DEFAULT = "llama-3.3-70b-versatile"
_REVIEW_OLLAMA_LIGHT_DEFAULT = "llama-3.3-70b-versatile"

# ChatOllama defaults usados por migration/test — review_agent passa modelos tier explícitos
_OLLAMA_GENERIC_DEFAULTS = frozenset({"llama3", "llama3:latest", ""})


def _resolver_modelo_patch_ollama(model: str) -> str:
    """Força OLLAMA_MODEL detectado quando o agente usa default genérico."""
    return OLLAMA_MODEL if model in _OLLAMA_GENERIC_DEFAULTS else model


def _detectar_ollama() -> tuple[bool, str]:
    """
    Verifica se o Ollama está rodando e retorna (disponível, nome_do_modelo).
    Testa a API REST em OLLAMA_HOST com timeout de 2s para não bloquear
    o start do pipeline caso o serviço não esteja ativo.
    """
    env_model = os.getenv("OLLAMA_MODEL", "").strip()
    try:
        url = f"{OLLAMA_HOST}/api/tags"
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())

        modelos = [m["name"] for m in data.get("models", [])]

        # Modelo especificado via env tem prioridade absoluta
        if env_model:
            if env_model in modelos or any(m.startswith(env_model) for m in modelos):
                return True, env_model
            print(f"  AVISO: OLLAMA_MODEL='{env_model}' não encontrado. "
                  f"Modelos disponíveis: {modelos}")
            return False, ""

        # Busca pelo modelo preferido na lista instalada
        for pref in _OLLAMA_MODEL_PREFS:
            if pref in modelos:
                return True, pref

        # Qualquer modelo llama disponível serve
        llamas = [m for m in modelos if "llama" in m.lower()]
        if llamas:
            return True, llamas[0]

        if modelos:
            print("  AVISO: Ollama está rodando mas não há modelo Llama instalado.")
            print("  Execute: ollama pull llama3.1")
        return False, ""

    except Exception:
        return False, ""


# ── Configuração do backend LLM ───────────────────────────────────────────────

print("\n  Detectando backend LLM disponível...")
OLLAMA_DISPONIVEL, OLLAMA_MODEL = _detectar_ollama()

GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY")
    or os.getenv("GROQ_KEY")
    or os.getenv("API_KEY")
)
API_3 = os.getenv("API_3")

if OLLAMA_DISPONIVEL:
    print(f"  [Ollama] Disponivel — modelo: {OLLAMA_MODEL}")
    print("  Migration usara Ollama (sem rate limit)")
    print("  Review usara Groq via API_3")
else:
    print("  [Ollama] Nao disponivel — usando Groq para todos os agentes")
    if not GROQ_API_KEY:
        print("  ERRO: GROQ_API_KEY nao encontrada e Ollama nao esta disponivel.")
        print("  Opcoes:")
        print("    1. Instale o Ollama: https://ollama.com  e execute: ollama pull llama3.1")
        print("    2. Defina GROQ_API_KEY no .env ou como variavel de ambiente")
        sys.exit(2)

if not GROQ_API_KEY and not OLLAMA_DISPONIVEL:
    # Caso impossível mas garante clareza
    print("  ERRO: nenhum backend disponivel.")
    sys.exit(2)


# ── Monkey-patch de ChatOllama → ChatGroq (somente se Ollama indisponivel) ────
# Quando Ollama está disponível, os agentes usam ChatOllama nativamente.
# Quando não está, substituímos ChatOllama por ChatGroq de forma transparente.

if not OLLAMA_DISPONIVEL:
    import langchain_groq

    def _make_groq_compat(model: str = "llama3", temperature: float = 0, **_kwargs):
        """
        Substitui ChatOllama por ChatGroq quando Ollama não está disponível.

        Escolha de modelo:
          - test_agent (node_analyzer envia código completo): 70B — janela grande
          - migration_agent (inferência + migração): 8B — prompts menores
        """
        import traceback
        stack = "".join(traceback.format_stack())
        use_large = "test_agent" in stack or "agent.py" in stack
        model_name = "llama-3.3-70b-versatile" if use_large else "llama-3.1-8b-instant"
        return langchain_groq.ChatGroq(
            model=model_name,
            temperature=temperature,
        )

    _fake_ollama_mod = types.ModuleType("langchain_ollama")
    _fake_ollama_mod.ChatOllama = _make_groq_compat  # type: ignore[attr-defined]
    sys.modules["langchain_ollama"] = _fake_ollama_mod

elif "langchain_ollama" not in sys.modules:
    # Ollama disponível: garante que langchain_ollama nativo é usado,
    # substituindo o model name para o modelo detectado
    try:
        from langchain_ollama import ChatOllama as _NativeChatOllama

        def _ollama_com_modelo_detectado(
            model: str = "llama3", temperature: float = 0, **kwargs
        ):
            """
            Usa o ChatOllama nativo mas força o modelo detectado
            (evita erros de modelo não encontrado quando o agente
            hardcoda "llama3" mas só "llama3.1" está instalado).
            """
            return _NativeChatOllama(
                model=_resolver_modelo_patch_ollama(model),
                temperature=temperature,
                **kwargs,
            )

        _patched_ollama_mod = types.ModuleType("langchain_ollama")
        _patched_ollama_mod.ChatOllama = _ollama_com_modelo_detectado  # type: ignore
        sys.modules["langchain_ollama"] = _patched_ollama_mod

    except ImportError:
        # langchain_ollama não instalado mas Ollama está rodando —
        # instala o pacote em tempo real como último recurso
        print("  AVISO: pacote langchain-ollama nao instalado.")
        print("  Execute: pip install langchain-ollama")
        print("  Continuando com Groq como fallback...")
        OLLAMA_DISPONIVEL = False
        import langchain_groq

        def _make_groq_compat(model: str = "llama3", temperature: float = 0, **_kwargs):  # type: ignore[misc]
            return langchain_groq.ChatGroq(
                model="llama-3.1-8b-instant",
                temperature=temperature,
            )

        _fake_ollama_mod = types.ModuleType("langchain_ollama")
        _fake_ollama_mod.ChatOllama = _make_groq_compat  # type: ignore
        sys.modules["langchain_ollama"] = _fake_ollama_mod

else:
    # langchain_ollama já estava importado — sobrescreve ChatOllama com modelo detectado
    from langchain_ollama import ChatOllama as _NativeChatOllama  # type: ignore[assignment]
    import langchain_ollama as _ollama_mod

    def _ollama_com_modelo_detectado(model: str = "llama3", temperature: float = 0, **kw):  # type: ignore[misc]
        return _NativeChatOllama(
            model=_resolver_modelo_patch_ollama(model),
            temperature=temperature,
            **kw,
        )

    _ollama_mod.ChatOllama = _ollama_com_modelo_detectado  # type: ignore[attr-defined, misc, assignment]


# ── Importação dinâmica dos agentes ───────────────────────────────────────────

def _import_migration_agent():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "langgraph_mig03", MIGRATION_DIR / "langgraph-mig03.py"
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = "langgraph_mig03"
    spec.loader.exec_module(mod)
    return mod


def _import_test_agent():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "test_agent_agent", TEST_AGENT_DIR / "agent" / "agent.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_review_agent():
    import importlib.util
    # Garante re-detecção de modelos Ollama quando env vars REVIEW_* foram definidas
    sys.modules.pop("review_agent", None)
    spec = importlib.util.spec_from_file_location(
        "review_agent", REVIEW_AGENT_DIR / "review-agent.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Funções de execução de cada etapa ─────────────────────────────────────────

def run_migration(
    codigo_original: str,
    num_examples: int = 10,
    feedback_revisao: str = "",
    codigo_migrado_atual: str = "",
) -> dict:
    """
    Executa o migration_agent.
    Quando feedback_revisao + codigo_migrado_atual são fornecidos, o agente vai
    direto ao nó 'refinar' sem re-migrar do zero.
    """
    print(f"\n{'='*60}")
    label = " (com feedback de teste)" if feedback_revisao else ""
    print(f"  ETAPA 1 — MIGRATION AGENT{label}")
    backend = f"Ollama ({OLLAMA_MODEL})" if OLLAMA_DISPONIVEL else "Groq (llama-3.1-8b-instant)"
    print(f"  Backend : {backend}")
    print(f"{'='*60}")
    t0 = time.time()

    mig = _import_migration_agent()

    if feedback_revisao:
        # Modo refinamento: não precisa de exemplos — o nó refinar usa prompt dedicado
        print("  Modo refinamento — pulando carregamento do dataset.")
        exemplos = []
    else:
        print(f"  Carregando {num_examples} exemplos do dataset...")
        exemplos = mig.carregar_exemplos_treino(num_examples)
        if not exemplos:
            print("  AVISO: nenhum exemplo carregado — usando prompt base sem few-shot.")

    prompt_sistema = mig.criar_prompt_treino(exemplos)
    agente = mig.criar_agente_migracao(exemplos, prompt_sistema)

    from langchain_core.messages import HumanMessage, AIMessage

    msg_content = (
        "Refinar migração urllib→requests com feedback do test agent"
        if feedback_revisao else
        "Migrar código urllib para requests"
    )

    resultado = agente.invoke({
        "messages":        [HumanMessage(content=msg_content)],
        "codigo_usuario":  codigo_original,
        "codigo_migrado":  codigo_migrado_atual,
        "feedback_revisao": feedback_revisao,
        "status":          "",
    })

    elapsed        = time.time() - t0
    codigo_migrado = resultado.get("codigo_migrado", "")
    status = resultado.get("status", "unknown")
    messages_text = [
        m.content for m in resultado.get("messages", [])
        if isinstance(m, AIMessage)
    ]

    print(f"  Status  : {status}")
    print(f"  Tempo   : {elapsed:.1f}s")
    print(f"  Linhas  : {len(codigo_migrado.splitlines())}")
    for msg in messages_text:
        print(f"  > {msg}")

    if not codigo_migrado.strip():
        print("  ERRO: Migration não gerou código.")
        sys.exit(3)

    return {
        "original_code":        codigo_original,
        "migrated_code":        codigo_migrado,
        "status":               status,
        "messages":             messages_text,
        "inferencia_semantica": resultado.get("inferencia_semantica", ""),
        "elapsed_s": elapsed,
        "backend": backend,
    }


def run_test(original_code: str, migrated_code: str) -> dict:
    """Executa o test_agent. Retorna: report, evaluation, decision, iteration."""
    print(f"\n{'='*60}")
    print("  ETAPA 2 — TEST AGENT")
    backend = f"Ollama ({OLLAMA_MODEL})" if OLLAMA_DISPONIVEL else "Groq (llama-3.3-70b-versatile)"
    print(f"  Backend : {backend}")
    print(f"{'='*60}")
    t0 = time.time()

    ta = _import_test_agent()
    print("  Construindo grafo de testes...")
    result = ta.run_agent(original_code, migrated_code)
    elapsed = time.time() - t0

    evaluation      = result.get("evaluation", {})
    router_decision = result.get("router_decision", {})
    needs_revision  = router_decision.get("needs_revision", False)
    decision        = "NEEDS_REVISION" if needs_revision else "APPROVED"

    print(f"  Decision  : {decision}")
    print(f"  Iteracoes : {result.get('iteration', 0)}")
    print(f"  Score     : {evaluation.get('overall', 'N/A')}")
    print(f"  Tempo     : {elapsed:.1f}s")

    return {
        "report": result.get("report", ""),
        "evaluation": evaluation,
        "router_decision": router_decision,
        "decision": decision,
        "iteration": result.get("iteration", 0),
        "elapsed_s": elapsed,
        "backend": backend,
    }


def run_review(original_code: str, migrated_code: str, max_retries: int = 3) -> dict:
    """
    Executa o review_agent diretamente (sem FastAPI).
    Todos os nós usam Groq llama-3.3-70b-versatile via API_3.
    Retry externo com backoff para erros que escapem dos retries internos.
    """
    print(f"\n{'='*60}")
    print("  ETAPA 3 — REVIEW AGENT")
    backend_rev = "Groq (llama-3.3-70b-versatile via API_3)"
    print(f"  Backend : {backend_rev}")
    print(f"{'='*60}")
    t0 = time.time()

    if not API_3:
        raise RuntimeError(
            "API_3 não encontrada — obrigatória para o review_agent.\n"
            "Defina API_3 no .env (mesma chave Groq) ou use --skip-review."
        )

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
                    print("\n  ATENCAO: Limite DIARIO de tokens Groq atingido.")
                    print("  Aguarde reset as 00:00 UTC ou use --skip-review.")
                    raise
                wait = 45 * attempt
                print(f"  Rate limit — aguardando {wait}s...")
                time.sleep(wait)
                if attempt == max_retries:
                    raise
            else:
                raise

    elapsed = time.time() - t0

    print(f"  Agentes acionados : {result.get('agentes_acionados', [])}")
    print(f"  Iteracoes critico : {result.get('iteracoes', 0)}")
    print(f"  Deve reprocessar  : {result.get('deve_reprocessar', False)}")
    print(f"  Achados semantica : {len(result.get('achados_semantica', []))}")
    print(f"  Achados seguranca : {len(result.get('achados_seguranca', []))}")
    print(f"  Achados lint      : {len(result.get('achados_lint', []))}")
    print(f"  Tempo             : {elapsed:.1f}s")

    backend_efetivo = result.get("backend", backend_rev)
    return {**result, "elapsed_s": elapsed, "backend": backend_efetivo}


# ── Pipeline principal ─────────────────────────────────────────────────────────

MAX_REVISION_LOOPS = 3   # evita loop infinito no test_agent
MAX_REVIEW_LOOPS   = 3   # máximo de iterações review → migration


def _formatar_feedback_teste(test_result: dict) -> str:
    """
    Converte o resultado do test_agent em feedback acionável para o migration_agent.
    Usa as regressões e sugestões do router, não o relatório textual.
    """
    rd          = test_result.get("router_decision", {})
    regressions = rd.get("regressions_detected", [])
    suggestions = rd.get("suggestions", [])
    reasons     = rd.get("reasons", [])
    equiv_rate  = rd.get("equivalence_rate", 0.0)

    linhas = [
        "The test agent found behavioral regressions in your urllib→requests migration.",
        f"Equivalence rate: {equiv_rate:.1f}% (threshold: 90%)\n",
    ]

    if regressions:
        linhas.append("## Tests that PASS on original but FAIL on migrated (must fix):")
        for r in regressions:
            linhas.append(f"  - {r}")
        linhas.append("")

    if reasons:
        linhas.append("## Root cause analysis:")
        for r in reasons:
            linhas.append(f"  - {r}")
        linhas.append("")

    if suggestions:
        linhas.append("## What to fix in the migration:")
        for s in suggestions:
            linhas.append(f"  - {s}")
        linhas.append("")

    linhas.append("Return ONLY the corrected Python code without any explanation or markdown.")
    return "\n".join(linhas)


def _formatar_feedback_review(review_result: dict) -> str:
    """
    Extrai achados do review e monta feedback acionável para o migration_agent.
    Prioriza P1 (críticos) e inclui P2; descarta o relatorio_final verboso.
    """
    achados_semantica = review_result.get("achados_semantica", [])
    achados_seguranca = review_result.get("achados_seguranca", [])
    achados_lint      = review_result.get("achados_lint", [])

    todos = achados_semantica + achados_seguranca + achados_lint
    p1 = [a for a in todos if "[P1]" in a]
    p2 = [a for a in todos if "[P2]" in a]

    linhas = [
        "The following issues were found in your urllib→requests migration.",
        "You MUST fix all [P1] items. [P2] items are also important.\n",
    ]

    if p1:
        linhas.append("## CRITICAL [P1] — Must fix:")
        linhas.extend(p1)
        linhas.append("")

    if p2:
        linhas.append("## Important [P2] — Should fix:")
        linhas.extend(p2)
        linhas.append("")

    if not p1 and not p2:
        # Fallback: qualquer achado presente
        linhas.append("## Issues found:")
        linhas.extend(todos or ["Review agent reported issues but provided no structured findings."])

    linhas.append(
        "\nReturn ONLY the corrected Python code without any explanation or markdown."
    )
    return "\n".join(linhas)

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Pipeline integrado: migration → test → review"
    )
    parser.add_argument(
        "--input", default=str(REPO_ROOT / "url.py"),
        help="Caminho para o arquivo Python urllib de entrada (padrao: url.py)",
    )
    parser.add_argument(
        "--output-dir", default=str(REPO_ROOT / ".pipeline_output"),
        help="Diretorio para salvar os artefatos de saida",
    )
    parser.add_argument(
        "--examples", type=int, default=30,
        help="Exemplos few-shot para o migration_agent (padrao: 30)",
    )
    parser.add_argument(
        "--skip-test", action="store_true",
        help="Pular etapa test_agent",
    )
    parser.add_argument(
        "--skip-review", action="store_true",
        help="Pular etapa review_agent (util quando cota diaria Groq esta esgotada)",
    )
    parser.add_argument(
        "--ollama-model", default="",
        help="Forcar um modelo Ollama especifico (ex: llama3.1, codellama)",
    )
    args = parser.parse_args()

    # Sobrescreve modelo se passado via flag
    global OLLAMA_MODEL
    if args.ollama_model:
        OLLAMA_MODEL = args.ollama_model

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    input_path = Path(args.input)

    if not input_path.exists():
        print(f"ERRO: Arquivo de entrada nao encontrado: {input_path}")
        sys.exit(1)

    original_code = input_path.read_text(encoding="utf-8")

    print(f"\n{'#'*60}")
    print("  PIPELINE DE INTEGRACAO")
    print(f"  Inicio : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Input  : {input_path}")
    print(f"  Output : {out_dir.resolve()}")
    print(f"  Ollama : {'SIM — ' + OLLAMA_MODEL if OLLAMA_DISPONIVEL else 'NAO (usando Groq)'}")
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

        # ── ETAPA 1: Migration ────────────────────────────────────────────────
        # Iteration 0: migração inicial.
        # Iterações seguintes: migrador recebe feedback estruturado do test_agent
        # via feedback_revisao e parte do código atual (vai direto ao nó refinar).
        if revision_loop == 0:
            migration_result = run_migration(current_original, num_examples=args.examples)
        else:
            test_feedback = _formatar_feedback_teste(test_result)
            migration_result = run_migration(
                current_original,
                num_examples=args.examples,
                feedback_revisao=test_feedback,
                codigo_migrado_atual=current_migrated,
            )

        current_migrated = migration_result["migrated_code"]

        # Save per-iteration artifact
        suffix = f"_iter{revision_loop}" if revision_loop > 0 else ""
        (out_dir / f"migrated_code{suffix}.py").write_text(current_migrated, encoding="utf-8")
        (out_dir / f"migration_result{suffix}.json").write_text(
            json.dumps({k: v for k, v in migration_result.items() if k != "migrated_code"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # ── ETAPA 2: Test ─────────────────────────────────────────────────────
        if args.skip_test:
            test_result = {"decision": "skipped"}
            break  # skip test → go straight to review

        if not OLLAMA_DISPONIVEL:
            print("\n  Aguardando 10s (rate limit Groq entre etapas)...")
            time.sleep(10)
        try:
            test_result = run_test(original_code, current_migrated)
            if test_result.get("report"):
                (out_dir / "test_report.md").write_text(
                    test_result["report"], encoding="utf-8"
                )
            (out_dir / "test_result.json").write_text(
                json.dumps(
                    {k: v for k, v in test_result.items() if k != "report"},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"\n  AVISO: test_agent falhou: {exc}")
            print("  Continuando para o review_agent...")
            test_result = {"error": str(exc), "decision": "error"}
            break

        # ── Router decision ───────────────────────────────────────────────────
        needs_revision = test_result.get("router_decision", {}).get("needs_revision", False)
        if not needs_revision:
            break  # test approved → exit loop

        if revision_loop >= MAX_REVISION_LOOPS:
            print(f"\n  ⚠️  Limite de {MAX_REVISION_LOOPS} revisões atingido — prosseguindo para review")
            break

        revision_loop += 1
        print(f"\n  🔄 Router: NEEDS_REVISION — iniciando iteração {revision_loop}/{MAX_REVISION_LOOPS}")
        if not OLLAMA_DISPONIVEL:
            print("  Aguardando 15s para respeitar rate limit do Groq...")
            time.sleep(15)

    # Save final migrated artifact (outside loop — reflects last iteration)
    (out_dir / "migrated_code.py").write_text(current_migrated, encoding="utf-8")
    (out_dir / "migration_result.json").write_text(
        json.dumps(
            {k: v for k, v in migration_result.items() if k != "migrated_code"},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # ── Loop review → migration com feedback ─────────────────────────────────
    # Fluxo: review → se deve_reprocessar → migration(feedback) → review → ...
    # Para quando review aprova OU MAX_REVIEW_LOOPS iterações são atingidas.
    review_loop = 0

    while True:
        if args.skip_review:
            print("\n  ETAPA 3 — REVIEW AGENT: pulada (--skip-review)")
            break

        wait_review = 10 if not OLLAMA_DISPONIVEL else 5
        label = f" (iteração {review_loop + 1}/{MAX_REVIEW_LOOPS + 1})" if review_loop > 0 else ""
        print(f"\n  Aguardando {wait_review}s antes do review{label} (Groq rate limit)...")
        time.sleep(wait_review)

        try:
            review_result = run_review(current_original, current_migrated)
        except Exception as exc:
            print(f"\n  AVISO: review_agent falhou: {exc}")
            review_result = {"error": str(exc)}
            break

        # Salva artefatos desta rodada de review
        suffix_rev = f"_review{review_loop}" if review_loop > 0 else ""
        if review_result.get("relatorio_final"):
            (out_dir / f"review_report{suffix_rev}.md").write_text(
                review_result["relatorio_final"], encoding="utf-8"
            )
        if review_result:
            (out_dir / f"review_result{suffix_rev}.json").write_text(
                json.dumps(
                    {k: v for k, v in review_result.items() if k != "relatorio_final"},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )

        if not review_result.get("deve_reprocessar"):
            print("  ✅ Review aprovou a migração — encerrando loop review→migration.")
            break

        if review_loop >= MAX_REVIEW_LOOPS:
            print(f"\n  ⚠️  Limite de {MAX_REVIEW_LOOPS} iterações review→migration atingido — encerrando.")
            break

        # ── Review pediu reprocessamento: roda migration → test → review ────────
        review_loop += 1
        review_feedback = _formatar_feedback_review(review_result)

        print(f"\n  🔁 Review solicitou reprocessamento (iteração {review_loop}/{MAX_REVIEW_LOOPS})"
              " — executando migrador → testador → revisor com feedback...")

        if not OLLAMA_DISPONIVEL:
            print("  Aguardando 10s antes do reprocessamento (rate limit Groq)...")
            time.sleep(10)

        # ── ETAPA 1 (re): Migration com feedback ──────────────────────────────
        try:
            mig = _import_migration_agent()
            print(f"  Carregando {args.examples} exemplos do dataset para reprocessamento...")
            exemplos = mig.carregar_exemplos_treino(args.examples)
            prompt_sistema = mig.criar_prompt_treino(exemplos)
            agente = mig.criar_agente_migracao(exemplos, prompt_sistema)

            from langchain_core.messages import HumanMessage, AIMessage

            resultado_reprocessado = agente.invoke({
                "messages":        [HumanMessage(content="Refinar migração urllib→requests com feedback do review")],
                "codigo_usuario":  current_original,
                "codigo_migrado":  current_migrated,   # código problemático → vai direto ao refinar
                "feedback_revisao": review_feedback,
                "status":          "",
            })

            codigo_reprocessado = resultado_reprocessado.get("codigo_migrado", "")
            if codigo_reprocessado.strip():
                current_migrated = codigo_reprocessado
                (out_dir / f"migrated_code_review{review_loop}.py").write_text(
                    current_migrated, encoding="utf-8"
                )
                reprocessamento_log = [
                    m.content for m in resultado_reprocessado.get("messages", [])
                    if isinstance(m, AIMessage)
                ]
                (out_dir / f"reprocessamento_log_review{review_loop}.json").write_text(
                    json.dumps({"iteracao": review_loop, "mensagens": reprocessamento_log},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  ✅ Migration {review_loop} concluído ({len(codigo_reprocessado.splitlines())} linhas)")
            else:
                print("  ⚠️  Migration não gerou novo código — mantendo versão anterior")

        except Exception as exc:
            print(f"  ❌ Erro ao re-migrar com feedback: {exc}")
            break

        # ── ETAPA 2 (re): Test após re-migração ───────────────────────────────
        if not args.skip_test:
            if not OLLAMA_DISPONIVEL:
                print("  Aguardando 10s (rate limit Groq entre etapas)...")
                time.sleep(10)
            try:
                test_result_re = run_test(current_original, current_migrated)
                if test_result_re.get("report"):
                    (out_dir / f"test_report_review{review_loop}.md").write_text(
                        test_result_re["report"], encoding="utf-8"
                    )
                (out_dir / f"test_result_review{review_loop}.json").write_text(
                    json.dumps(
                        {k: v for k, v in test_result_re.items() if k != "report"},
                        ensure_ascii=False, indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"  Test {review_loop}: {test_result_re.get('decision', 'unknown')}")
            except Exception as exc:
                print(f"  AVISO: test_agent falhou na iteração {review_loop}: {exc}")

    # Salva o artefato final consolidado (última versão após todos os loops)
    (out_dir / "review_report.md").write_text(
        review_result.get("relatorio_final", ""), encoding="utf-8"
    )
    (out_dir / "review_result.json").write_text(
        json.dumps(
            {k: v for k, v in review_result.items() if k != "relatorio_final"},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # ── Sumário final ─────────────────────────────────────────────────────────
    total_elapsed = time.time() - pipeline_start

    pipeline_summary = {
        "pipeline_start": datetime.now().isoformat(),
        "total_elapsed_s": round(total_elapsed, 1),
        "input_file": str(input_path),
        "backend": {
            "ollama_disponivel": OLLAMA_DISPONIVEL,
            "ollama_model": OLLAMA_MODEL if OLLAMA_DISPONIVEL else None,
            "groq_usado_em": (
                ["migration", "review"] if not OLLAMA_DISPONIVEL
                else (["review"] if not args.skip_review else [])
            ),
            "review_api_key": "API_3" if not args.skip_review else None,
        },
        "stages": {
            "migration": {
                "status": migration_result.get("status"),
                "elapsed_s": migration_result.get("elapsed_s"),
                "migrated_lines": len(current_migrated.splitlines()),
                "backend": migration_result.get("backend"),
            },
            "test": {
                "decision": test_result.get("decision"),
                "iteration": test_result.get("iteration"),
                "elapsed_s": test_result.get("elapsed_s"),
                "error": test_result.get("error"),
                "backend": test_result.get("backend"),
            },
            "review": {
                "iteracoes_review_migration": review_loop,
                "aprovado": not review_result.get("deve_reprocessar"),
                "achados_total": (
                    len(review_result.get("achados_semantica") or [])
                    + len(review_result.get("achados_seguranca") or [])
                    + len(review_result.get("achados_lint") or [])
                ),
                "elapsed_s": review_result.get("elapsed_s"),
                "error": review_result.get("error"),
                "backend": review_result.get("backend"),
            },
        },
    }

    (out_dir / "pipeline_summary.json").write_text(
        json.dumps(pipeline_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{'#'*60}")
    print("  PIPELINE CONCLUIDO")
    print(f"  Tempo total : {total_elapsed:.1f}s")
    print(f"  Migration   : {migration_result.get('status')}")
    print(f"  Test        : {test_result.get('decision', 'skipped')}")
    if review_result.get("error"):
        print(f"  Review      : erro — {review_result['error'][:80]}")
    elif args.skip_review:
        print("  Review      : pulado")
    else:
        status_rev = "aprovado" if not review_result.get("deve_reprocessar") else "reprocessar (limite atingido)"
        iters_label = f" ({review_loop} reprocessamento{'s' if review_loop != 1 else ''})" if review_loop > 0 else ""
        print(f"  Review      : {status_rev}{iters_label}")
    print(f"  Artefatos   : {out_dir.resolve()}")
    print(f"{'#'*60}\n")

    relatorio = review_result.get("relatorio_final", "") if review_result else ""
    if relatorio:
        preview = relatorio[:1200]
        print("── PREVIA DO RELATORIO DE REVISAO ──────────────────────")
        print(preview)
        if len(relatorio) > 1200:
            print(f"... [+{len(relatorio)-1200} chars — ver {out_dir}/review_report.md]")
        print("────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()