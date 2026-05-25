"""
CodeReviewAgent – Orquestração LangGraph com Reflection Loop

Fluxo:
  Entrada → [no_parser] → [no_classificador] → [no_roteador] ──fan-out──►
  [no_semantico]  ─┐
  [no_seguranca]   ├──► [no_critico] ──► loop ou [relatorio_final] → END
  [no_lint/Ruff]   ┘

O no_critico atua como QA da revisão (Reflection): aprova ou rejeita com
uma crítica estruturada. Na iteração máxima, se a qualidade ainda for ruim,
sinaliza `deve_reprocessar = True` para que o migration_agent refaça a migração.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request as _urllib_request
from collections import Counter
from pathlib import Path
from typing import Any, Literal, TypedDict

from fastapi import FastAPI, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel


# ─────────────────────────────────────────────────────────────────────────────
# Carregamento de Prompts
# ─────────────────────────────────────────────────────────────────────────────

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPTS: dict[str, list[str]] = {}


def _load_prompts() -> None:
    """Carrega todos os arquivos JSON da pasta prompts/ em memória na inicialização."""
    for path in sorted(_PROMPTS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        _PROMPTS[path.stem] = data["template"]


def _render(key: str, **kwargs: str) -> str:
    """
    Renderiza um template de prompt substituindo os placeholders <<variavel>>
    pelos valores fornecidos.
    """
    text = "\n".join(_PROMPTS[key])
    for var, val in kwargs.items():
        text = text.replace(f"<<{var}>>", val)
    return text


_load_prompts()


# ─────────────────────────────────────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────────────────────────────────────

def _strip_md_fences(text: str) -> str:
    """Remove markdown code fences que o LLM às vezes envolve ao redor do JSON."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text.strip()


# ── LLM backend strategy ──────────────────────────────────────────────────────
#
# Priority 1 — Ollama (local):   zero cost, zero rate limits, zero API keys.
#   All nodes use the same local model configured via REVIEW_OLLAMA_MODEL.
#
# Priority 2 — Cloud fallback (when Ollama is not running):
#   Light nodes  → Groq llama-3.1-8b-instant  (fast, low token consumption)
#   Heavy nodes  → Google Gemini 2.5 Flash     (deep reasoning, generous quota)
#
# Detection runs once at startup; no overhead per node call.

_MODEL_GROQ_LIGHT   = "llama-3.1-8b-instant"
_MODEL_GEMINI_HEAVY = "gemini-2.5-flash"
_OLLAMA_HOST        = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Two-tier Ollama strategy: heavy nodes get a larger/code-specific model,
# light nodes get a smaller/faster model.
# Override via env vars:
#   REVIEW_OLLAMA_MODEL_HEAVY=qwen2.5-coder:14b  (parser, semantic, security, critic)
#   REVIEW_OLLAMA_MODEL_LIGHT=qwen2.5-coder:3b   (classifier, lint, report)
# Single-model fallback: REVIEW_OLLAMA_MODEL applies to all nodes.
_MODEL_OLLAMA_HEAVY = os.getenv(
    "REVIEW_OLLAMA_MODEL_HEAVY",
    os.getenv("REVIEW_OLLAMA_MODEL", "llama3.1:8b"),
)
_MODEL_OLLAMA_LIGHT = os.getenv(
    "REVIEW_OLLAMA_MODEL_LIGHT",
    os.getenv("REVIEW_OLLAMA_MODEL", "llama3.1:8b"),
)
# Legacy alias — still used by _detectar_ollama_local to verify availability
_MODEL_OLLAMA = _MODEL_OLLAMA_HEAVY

_LIGHT_NODES: frozenset[str] = frozenset({
    "no_classificador",  # diff pattern matching → agent list
    "no_lint",           # interprets deterministic Ruff output
    "relatorio_final",   # consolidates already-structured findings into Markdown
})

_HEAVY_NODES: frozenset[str] = frozenset({
    "no_parser",     # receives the full diff (can have thousands of tokens)
    "no_semantico",  # multi-step reasoning about functional equivalence
    "no_seguranca",  # security domain analysis
    "no_critico",    # meta-evaluation of finding quality (reflection)
})

# Substitution of full code files in iterations 2+.
# The raw_diff with unified=5 already contains the (-/+) lines and sufficient context;
# resending the entire code would waste ~60% of the tokens.
_REFINAMENTO_NOTA = (
    "[REFINEMENT — code identical to iteration 1. "
    "Consult the raw_diff for the changed lines and focus on the rejection_reason.]"
)

# Regex that matches findings in the standardized format:
#   - [PREFIX][Px] `symbol` (line N) — description. Trigger: ...
# Also accepts the canonical "no findings" lines produced by each agent.
_PADRAO_ACHADO = re.compile(r"^-\s*\[")


def _resolver_modelo_ollama(desejado: str, disponiveis: list[str]) -> str:
    """
    Resolves the best available Ollama model for a requested model name.
    Priority: exact match → prefix match → first available.
    """
    if desejado in disponiveis:
        return desejado
    match = next((m for m in disponiveis if m.startswith(desejado.split(":")[0])), None)
    if match:
        return match
    return disponiveis[0]


def _detectar_ollama_local() -> tuple[bool, str, str]:
    """
    Checks once at startup whether Ollama is running and resolves both
    the heavy and light model names against what is actually installed.
    Returns (available, heavy_model, light_model).
    Uses a 2s timeout so a missing Ollama service never blocks startup.
    """
    try:
        url = f"{_OLLAMA_HOST}/api/tags"
        with _urllib_request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())
        modelos = [m["name"] for m in data.get("models", [])]
        if not modelos:
            return False, "", ""

        heavy = _resolver_modelo_ollama(_MODEL_OLLAMA_HEAVY, modelos)
        light = _resolver_modelo_ollama(_MODEL_OLLAMA_LIGHT, modelos)
        return True, heavy, light
    except Exception:
        return False, "", ""


# Module-level detection — runs once when the module is imported.
_OLLAMA_DISPONIVEL, _OLLAMA_HEAVY_ATIVO, _OLLAMA_LIGHT_ATIVO = _detectar_ollama_local()

if _OLLAMA_DISPONIVEL:
    if _OLLAMA_HEAVY_ATIVO == _OLLAMA_LIGHT_ATIVO:
        print(f"  [review_agent] Ollama detected — single model: {_OLLAMA_HEAVY_ATIVO}")
    else:
        print(f"  [review_agent] Ollama detected — heavy: {_OLLAMA_HEAVY_ATIVO} / light: {_OLLAMA_LIGHT_ATIVO}")
else:
    print("  [review_agent] Ollama not detected — using Gemini (heavy) + Groq (light)")


def _get_llm(node: str = "default") -> ChatOllama | ChatGroq | ChatGoogleGenerativeAI:
    """
    Returns the appropriate LLM for the given node.

    When Ollama is running locally — two-tier strategy:
      Heavy nodes → REVIEW_OLLAMA_MODEL_HEAVY (default: llama3.1:8b)
        • no_parser, no_semantico, no_seguranca, no_critico
        Recommended: qwen2.5-coder:14b or qwen2.5-coder:32b
      Light nodes → REVIEW_OLLAMA_MODEL_LIGHT (default: same as heavy)
        • no_classificador, no_lint, relatorio_final
        Recommended: qwen2.5-coder:3b or llama3.2:3b

    When Ollama is unavailable (cloud fallback):
      Heavy nodes  → Gemini 2.5 Flash   (deep reasoning, generous quota)
      Light nodes  → Groq llama-3.1-8b  (fast, low token consumption)
    """
    if _OLLAMA_DISPONIVEL:
        ollama_model = (
            _OLLAMA_HEAVY_ATIVO if node in _HEAVY_NODES else _OLLAMA_LIGHT_ATIVO
        )
        return ChatOllama(
            model=ollama_model,
            base_url=_OLLAMA_HOST,
            temperature=0.0,
        )

    if node in _HEAVY_NODES:
        if not os.getenv("GOOGLE_API_KEY"):
            raise ValueError(
                "GOOGLE_API_KEY not found. Either start Ollama or set this env var."
            )
        return ChatGoogleGenerativeAI(
            model=_MODEL_GEMINI_HEAVY,
            temperature=0.0,
        )

    if not os.getenv("GROQ_API_KEY"):
        raise ValueError(
            "GROQ_API_KEY not found. Either start Ollama or set this env var."
        )
    return ChatGroq(
        model=_MODEL_GROQ_LIGHT,
        temperature=0.0,
    )


def _invoke_com_retry(llm: Any, prompt: str, max_tentativas: int = 3) -> Any:
    """
    Invoca o LLM com retry exponencial em caso de rate limit (429/413).

    Este wrapper é a rede de segurança para erros de cota que escapam das
    estratégias primárias (Gemini para nós pesados, Groq 8B para nós leves).
    Não adiciona latência quando não há rate limit — o sleep só ocorre após erro.

    Backoff: 30s → 60s → 120s (dobrando a cada tentativa).
    """
    for tentativa in range(1, max_tentativas + 1):
        try:
            return llm.invoke(prompt)
        except Exception as exc:
            err = str(exc)
            is_rate_limit = (
                "429" in err
                or "413" in err
                or "rate_limit" in err.lower()
                or "quota" in err.lower()
                or "resource_exhausted" in err.lower()
            )
            if is_rate_limit and tentativa < max_tentativas:
                wait = 30 * (2 ** (tentativa - 1))  # 30s, 60s, 120s
                print(f"  [retry] Rate limit on LLM (attempt {tentativa}/{max_tentativas})"
                      f" — waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("_invoke_com_retry: max_tentativas deve ser >= 1")


# ─────────────────────────────────────────────────────────────────────────────
# Estado Compartilhado
# ─────────────────────────────────────────────────────────────────────────────

class CodeReviewState(TypedDict):
    """Estado completo do fluxo de revisão de migração de código."""

    # Entradas
    codigo_original: str          # Código legado (pré-migração)
    codigo_migrado: str           # Código resultante da migração

    # Saída do Parser
    raw_diff: str                 # Diff unificado gerado pelo git diff --no-index (fonte primária)
    diff_estruturado: dict        # Funções/classes/dependências alteradas, adicionadas, removidas

    # Decisão do Classificador
    agentes_acionados: list[str]  # Subconjunto de ["semantica", "seguranca", "lint"]

    # Achados por especialidade (resetados a cada iteração pelo Roteador)
    achados_semantica: list[str]
    achados_seguranca: list[str]
    achados_lint: list[str]

    # Feedback do Nó Crítico para os agentes refinarem na próxima iteração
    motivo_rejeicao: str

    # Controle de qualidade
    status_qualidade: str   # "" | "approved" | "requires_refinement"
    iteracao: int           # Incrementado pelo Roteador; máximo efetivo = 3
    deve_reprocessar: bool  # True = no_critico sinaliza que o migration_agent deve refazer

    # Cache de iteração: evitam reenvio de código completo nas rodadas 2+
    # Inicializados como None em _executar_grafo; preenchidos pelo no_lint na iter 1.
    ruff_config:       dict | None   # Config Ruff inferida na iter 1
    ruff_novos_issues: list | None   # Issues novos do Ruff na iter 1

    # Histórico de achados por iteração (preenchido pelo no_roteador antes de limpar)
    # Lista de dicts: [{"iteracao": N, "semantica": [...], "seguranca": [...], "lint": [...]}]
    historico_achados: list

    # Artefato final entregue ao humano
    relatorio_final: str


# ─────────────────────────────────────────────────────────────────────────────
# Nó 1 – Parser (Estrutural)
# ─────────────────────────────────────────────────────────────────────────────

def _run_git_diff(codigo_original: str, codigo_migrado: str) -> str | None:
    """
    Compara os dois trechos de código usando `git diff --no-index`, que opera
    em arquivos arbitrários sem precisar de um repositório git ativo.

    Retorna o diff em formato unificado (unified diff), ou None se o git não
    estiver disponível no PATH ou ocorrer timeout.

    Exit codes do git diff: 0 = sem diferenças, 1 = há diferenças — ambos são
    válidos; qualquer outro código indica erro real.
    """
    tmp_original: str | None = None
    tmp_migrado:  str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_original.py", encoding="utf-8", delete=False
        ) as f:
            f.write(codigo_original)
            tmp_original = f.name

        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_migrado.py", encoding="utf-8", delete=False
        ) as f:
            f.write(codigo_migrado)
            tmp_migrado = f.name

        result = subprocess.run(
            [
                "git", "diff", "--no-index",
                "--unified=5",          # 5 linhas de contexto ao redor de cada mudança
                "--diff-algorithm=histogram",  # algoritmo mais preciso para código
                tmp_original, tmp_migrado,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode in (0, 1):
            return result.stdout.strip() or "(sem diferenças entre os arquivos)"
        return None

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        for path in (tmp_original, tmp_migrado):
            if path and os.path.exists(path):
                os.unlink(path)


def no_parser(state: CodeReviewState) -> dict:
    """
    Compara código original e migrado, extrai o diff estruturado e mapeia
    o impacto (funções/classes adicionadas, alteradas ou removidas).

    Pipeline interno:
    1. `git diff --no-index` gera o diff exato e determinístico (zero tokens).
    2. O LLM recebe APENAS o raw_diff como entrada — os códigos completos
       não são enviados, reduzindo o consumo de tokens em ~60%.
    3. Fallback: se git não estiver disponível, os códigos são embutidos
       diretamente no campo raw_diff para análise pelo LLM.
    """
    raw_diff = _run_git_diff(state["codigo_original"], state["codigo_migrado"])

    if raw_diff:
        raw_diff_para_prompt = raw_diff
    else:
        # Git unavailable: embed full code in diff context so the LLM can still
        # compare the files. The prompt variable name stays the same.
        raw_diff_para_prompt = (
            "(git not available — compare the two code listings below directly)\n\n"
            "### ORIGINAL CODE:\n```python\n"
            + state["codigo_original"]
            + "\n```\n\n### MIGRATED CODE:\n```python\n"
            + state["codigo_migrado"]
            + "\n```"
        )

    llm = _get_llm("no_parser")
    prompt = _render("parser", raw_diff=raw_diff_para_prompt)
    response = _invoke_com_retry(llm, prompt)
    try:
        diff = json.loads(_strip_md_fences(response.content))
    except json.JSONDecodeError:
        diff = {"raw": response.content, "parse_error": True}

    return {
        "raw_diff":         raw_diff or "",
        "diff_estruturado": diff,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Nó 2 – Classificador (Semântico Leve)
# ─────────────────────────────────────────────────────────────────────────────

def no_classificador(state: CodeReviewState) -> dict:
    """
    Lê o diff estruturado e decide quais agentes especialistas devem ser
    acionados, preenchendo `agentes_acionados`.

    Quando o parser retorna parse_error:
    1. Tenta recuperar o JSON da resposta bruta usando regex.
    2. Se a recuperação falhar mas raw_diff estiver disponível, usa o LLM
       para classificar diretamente a partir do raw_diff.
    3. Se nenhuma das estratégias funcionar, interrompe com RuntimeError
       informativo — sem análise parcial silenciosa.
    """
    diff = state["diff_estruturado"]

    if diff.get("parse_error"):
        raw_response = diff.get("raw", "")
        raw_diff     = state.get("raw_diff", "")

        # Strategy 1: try to recover a valid JSON from the raw LLM response.
        if raw_response:
            json_match = re.search(
                r'\{[^{}]*"altered_functions"[^{}]*\}', raw_response, re.DOTALL
            )
            if json_match:
                try:
                    recovered = json.loads(json_match.group())
                    diff_str  = json.dumps(recovered, ensure_ascii=False, indent=2)
                    llm       = _get_llm("no_classificador")
                    response  = _invoke_com_retry(llm, _render("classificador", diff_str=diff_str))
                    agentes   = json.loads(_strip_md_fences(response.content)).get("agents", ["semantics", "security", "lint"])
                    print(f"  [no_classificador] Parser JSON recovered — triggering: {agentes}")
                    return {"agentes_acionados": agentes}
                except Exception:
                    pass  # fall through to strategy 2

        # Strategy 2: classify based on raw_diff (still deterministic, no code needed).
        if raw_diff:
            print(
                "  [no_classificador] WARNING: parser produced invalid JSON. "
                f"Raw response preview: {raw_response[:120] if raw_response else '(empty)'}. "
                "Attempting classification from raw_diff."
            )
            llm      = _get_llm("no_classificador")
            fallback = json.dumps({"parse_error": True, "raw_diff_preview": raw_diff[:800]},
                                  ensure_ascii=False, indent=2)
            try:
                response = _invoke_com_retry(llm, _render("classificador", diff_str=fallback))
                agentes  = json.loads(_strip_md_fences(response.content)).get("agents", ["semantics", "security", "lint"])
                return {"agentes_acionados": agentes}
            except Exception as exc:
                raise RuntimeError(
                    f"Classifier recovery also failed: {exc}. "
                    f"Parser raw response: {raw_response[:200] if raw_response else '(empty)'}."
                ) from exc

        # Strategy 3: nothing works — interrupt with an informative error.
        raise RuntimeError(
            "Parser produced an invalid structured diff AND git diff is unavailable. "
            "Ensure git is installed and the input code is valid Python. "
            f"Parser raw response: {raw_response[:200] if raw_response else '(empty)'}."
        )

    llm      = _get_llm("no_classificador")
    diff_str = json.dumps(diff, ensure_ascii=False, indent=2)
    response = _invoke_com_retry(llm, _render("classificador", diff_str=diff_str))
    try:
        parsed  = json.loads(_strip_md_fences(response.content))
        agentes = parsed.get("agents", ["semantics"])
    except (json.JSONDecodeError, AttributeError):
        agentes = ["semantics", "security", "lint"]

    return {"agentes_acionados": agentes}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 3 – Roteador (preparação de iteração + fan-out via Send)
# ─────────────────────────────────────────────────────────────────────────────

def no_roteador(state: CodeReviewState) -> dict:
    """
    Prepara o estado para uma nova rodada de análise:
    - Salva os achados da iteração atual em `historico_achados` antes de limpar.
    - Incrementa o contador de iteração.
    - Limpa os achados anteriores para evitar acúmulo entre rodadas.

    O histórico permite que o relatório final e inspeções externas comparem
    como os achados evoluíram a cada iteração do reflection loop.
    """
    iteracao_atual = state.get("iteracao", 0)

    historico = list(state.get("historico_achados", []))
    if iteracao_atual > 0:
        historico.append({
            "iteracao":          iteracao_atual,
            "achados_semantica": list(state.get("achados_semantica", [])),
            "achados_seguranca": list(state.get("achados_seguranca", [])),
            "achados_lint":      list(state.get("achados_lint", [])),
        })

    return {
        "iteracao":          iteracao_atual + 1,
        "historico_achados": historico,
        "achados_semantica": [],
        "achados_seguranca": [],
        "achados_lint":      [],
    }


def _despachar_agentes(state: CodeReviewState) -> list[Send]:
    """
    Função de roteamento condicional: gera um `Send` para cada agente listado
    em `agentes_acionados`, possibilitando execução paralela.
    Caso a lista esteja vazia, encaminha diretamente ao Nó Crítico.
    """
    mapa_agentes = {
        "semantics": "no_semantico",
        "security":  "no_seguranca",
        "lint":      "no_lint",
    }
    envios = [
        Send(mapa_agentes[agente], state)
        for agente in state.get("agentes_acionados", [])
        if agente in mapa_agentes
    ]
    return envios if envios else [Send("no_critico", state)]


# ─────────────────────────────────────────────────────────────────────────────
# Nó 4a – no_semantico
# ─────────────────────────────────────────────────────────────────────────────

def no_semantico(state: CodeReviewState) -> dict:
    """
    Analisa a equivalência semântica entre o código original e o migrado.

    Recebe APENAS o diff estruturado (JSON) e o raw_diff (git) — os códigos
    completos nunca são enviados, economizando tokens em todas as iterações.
    O raw_diff fornece números de linha exatos por achado (técnica PR-Agent).
    Em iterações de refinamento, inclui `motivo_rejeicao` para refinar o foco.

    Usa Gemini 2.5 Flash: raciocínio multi-step sobre equivalência funcional.
    """
    llm = _get_llm("no_semantico")
    critica = (
        f"\n⚠️  FEEDBACK FROM PREVIOUS ITERATION — address the points below:\n{state['motivo_rejeicao']}"
        if state.get("motivo_rejeicao")
        else ""
    )
    diff_str = json.dumps(state["diff_estruturado"], ensure_ascii=False, indent=2)

    prompt = _render(
        "agente_semantica",
        critica=critica,
        diff_str=diff_str,
        raw_diff=state.get("raw_diff") or "(git diff not available)",
    )
    response = _invoke_com_retry(llm, prompt)
    achados = [
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ]
    return {"achados_semantica": achados or ["- [INFO][P3] No relevant semantic findings."]}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 4b – no_seguranca
# ─────────────────────────────────────────────────────────────────────────────

def no_seguranca(state: CodeReviewState) -> dict:
    """
    Audita riscos de segurança introduzidos pela migração.

    Recebe APENAS o diff estruturado (JSON) e o raw_diff — os códigos completos
    nunca são enviados em nenhuma iteração.
    O raw_diff fornece números de linha exatos por achado (técnica PR-Agent).
    Em iterações de refinamento, inclui `motivo_rejeicao` para refinar o foco.

    Usa Gemini 2.5 Flash: análise de domínio em segurança de software.
    """
    llm = _get_llm("no_seguranca")
    critica = (
        f"\n⚠️  FEEDBACK FROM PREVIOUS ITERATION — address the points below:\n{state['motivo_rejeicao']}"
        if state.get("motivo_rejeicao")
        else ""
    )
    diff_str = json.dumps(state["diff_estruturado"], ensure_ascii=False, indent=2)

    prompt = _render(
        "agente_seguranca",
        critica=critica,
        diff_str=diff_str,
        raw_diff=state.get("raw_diff") or "(git diff not available)",
    )
    response = _invoke_com_retry(llm, prompt)
    achados = [
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ]
    return {"achados_seguranca": achados or ["- [INFO][P3] No relevant security findings."]}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 4c – no_lint  (tool use determinístico via Ruff + interpretação LLM)
# ─────────────────────────────────────────────────────────────────────────────

# Rules that must always be active regardless of what the LLM infers from the original code.
# F401 — unused imports (catches leftover imports after migration removes the usage)
# B006 — mutable default arguments (common migration mistake)
# S603/S605/S607 — subprocess/os.system with dynamic arguments (shell injection)
_RUFF_MANDATORY_RULES: frozenset[str] = frozenset({"F401", "B006", "S603", "S605", "S607"})


def _inferir_config_ruff(codigo_original: str, llm: ChatOllama | ChatGroq | ChatGoogleGenerativeAI) -> dict:
    """
    Usa o LLM para inferir o estilo implícito do código original e retorna
    uma configuração Ruff ajustada (line_length, regras, indent_width).

    O conjunto de regras retornado pelo LLM é complementado com
    _RUFF_MANDATORY_RULES que nunca podem ser omitidas, independente do estilo
    inferido (ex: F401 para imports órfãos, S605 para injeção via os.system).
    """
    prompt = _render("agente_lint_config", codigo_original=codigo_original)
    response = _invoke_com_retry(llm, prompt)
    defaults = {"line_length": 88, "select": ["E", "W", "F", "I"], "ignore": [], "indent_width": 4}
    try:
        config = json.loads(_strip_md_fences(response.content))
        llm_select = config.get("select", defaults["select"])
        # Merge LLM-inferred rules with mandatory rules — LLM cannot opt out of these.
        merged_select = sorted(set(llm_select) | _RUFF_MANDATORY_RULES)
        return {
            "line_length":  int(config.get("line_length", defaults["line_length"])),
            "select":       merged_select,
            "ignore":       config.get("ignore", defaults["ignore"]),
            "indent_width": int(config.get("indent_width", defaults["indent_width"])),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        merged_defaults = sorted(set(defaults["select"]) | _RUFF_MANDATORY_RULES)
        return {**defaults, "select": merged_defaults}


def _run_ruff(code_str: str, config: dict) -> list[dict]:
    """
    Salva `code_str` em um arquivo temporário, executa o Ruff com a configuração
    dinâmica e devolve a lista de issues em formato JSON.
    O arquivo temporário é removido mesmo em caso de erro.
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(code_str)
            tmp_path = tmp.name

        select = ",".join(config.get("select", ["E", "W", "F", "I"]))
        cmd = [
            "ruff", "check", tmp_path,
            "--output-format=json",
            f"--line-length={config.get('line_length', 88)}",
            f"--indent-width={config.get('indent_width', 4)}",
            f"--select={select}",
            "--no-cache",
        ]
        ignore = config.get("ignore", [])
        if ignore:
            cmd.append(f"--ignore={','.join(ignore)}")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.stdout.strip():
            issues = json.loads(result.stdout)
            # Remove o path absoluto do arquivo temporário de cada issue
            for issue in issues:
                issue.pop("filename", None)
            return issues
        return []

    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _filtrar_novos_issues(
    issues_original: list[dict], issues_migrado: list[dict]
) -> list[dict]:
    """
    Filtro de regressão: isola apenas os issues do código migrado que NÃO
    existiam no original, comparando por (code, message).

    Usa Counter para tratar duplicatas corretamente: se o original já tinha
    N ocorrências idênticas de um issue, somente as ocorrências além de N
    são consideradas novas.
    """
    presentes_original = Counter(
        (i.get("code", ""), i.get("message", "")) for i in issues_original
    )
    consumidos: Counter = Counter()
    novos: list[dict] = []

    for issue in issues_migrado:
        chave = (issue.get("code", ""), issue.get("message", ""))
        if consumidos[chave] < presentes_original[chave]:
            consumidos[chave] += 1  # issue pré-existente — ignorar
        else:
            novos.append(issue)     # issue novo introduzido pela migração

    return novos


def no_lint(state: CodeReviewState) -> dict:
    """
    Valida lint e estilo do código migrado usando Ruff como tool use determinístico.

    Pipeline interno:
    1. LLM infere o estilo implícito do código original → config Ruff dinâmica.
    2. Ruff é executado em ambos os códigos (original e migrado) via subprocess.
    3. Filtro de regressão: isola apenas os *novos* issues introduzidos.
    4. LLM interpreta os novos issues avaliando severidade e contexto.

    Achados são validados com _PADRAO_ACHADO para garantir o formato padronizado:
      - [BLOCKER][P0] / [WARNING][P2] / [COSMETIC][P3] `symbol` (line N) — desc.

    Usa modelo 8B: o Ruff já fez a detecção determinística; o LLM só interpreta.
    """
    llm = _get_llm("no_lint")

    iteracao = state.get("iteracao", 1)

    if iteracao > 1 and state.get("ruff_config") is not None:
        # Iteração 2+: o código não mudou, então o Ruff produziria exatamente
        # os mesmos resultados. Reutiliza config e issues cacheados da iter 1,
        # poupando 2 chamadas ao Ruff + 1 chamada LLM (_inferir_config_ruff).
        ruff_config  = state["ruff_config"]
        novos_issues = state.get("ruff_novos_issues", [])
    else:
        # Iteração 1: infere estilo, roda Ruff em ambos os arquivos e filtra.
        ruff_config     = _inferir_config_ruff(state["codigo_original"], llm)
        issues_original = _run_ruff(state["codigo_original"], ruff_config)
        issues_migrado  = _run_ruff(state["codigo_migrado"],  ruff_config)
        novos_issues    = _filtrar_novos_issues(issues_original, issues_migrado)

    if not novos_issues:
        return {
            "achados_lint":      ["- No new lint/style issues introduced by the migration."],
            "ruff_config":       ruff_config,
            "ruff_novos_issues": novos_issues,
        }

    # LLM interprets new issues (severity + context).
    # In iterations 2+, codigo_migrado is not resent — the LLM focuses on the critique.
    critica = (
        f"\n⚠️  FEEDBACK FROM PREVIOUS ITERATION — address the points below:\n{state['motivo_rejeicao']}"
        if state.get("motivo_rejeicao")
        else ""
    )
    codigo_migrado_ctx = (
        "[REFINEMENT — same code as iteration 1. Focus on the issues and the feedback below.]"
        if iteracao > 1
        else state["codigo_migrado"]
    )
    prompt = _render(
        "agente_lint_interpretacao",
        critica=critica,
        novos_issues=json.dumps(novos_issues, ensure_ascii=False, indent=2),
        estilo_inferido=json.dumps(ruff_config, ensure_ascii=False, indent=2),
        codigo_migrado=codigo_migrado_ctx,
    )
    response = _invoke_com_retry(llm, prompt)
    achados = [
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ]
    return {
        "achados_lint":      achados or ["- [INFO][P3] No relevant new lint/style issues identified."],
        "ruff_config":       ruff_config,
        "ruff_novos_issues": novos_issues,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Nó 5 – Nó Crítico (Reflection / QA da Revisão)
# ─────────────────────────────────────────────────────────────────────────────

_MAX_ITERACOES = 3

# Padrões que indicam achados de alta severidade.
# Usa colchetes para evitar falso positivo em substrings como "P10", "P11", etc.
_ALTA_SEVERIDADE = ("[P0]", "[P1]")


def _tem_achado_critico(achados: list[str]) -> bool:
    """
    Retorna True se algum achado contém [P0] ou [P1] (com colchetes).
    Retorna False quando todos são [P2]/[P3] ou a lista está vazia/apenas negativas.
    """
    return any(
        sev in linha
        for linha in achados
        for sev in _ALTA_SEVERIDADE
    )


def no_critico(state: CodeReviewState) -> dict:
    """
    Ponto de convergência e Reflection da revisão.

    - Saída antecipada: se NENHUM achado contiver [P0] ou [P1], aprova
      imediatamente sem chamar o LLM — achados P2/P3 não justificam iterações extras.
    - Aprova se os achados forem precisos, específicos e acionáveis.
    - Rejeita com `motivo_rejeicao` se houver falsos positivos, achados vagos
      ou redundâncias em achados [P0]/[P1], para que os agentes refinem.
    - Na iteração máxima, avalia normalmente com o LLM: se ainda rejeitar,
      sinaliza `deve_reprocessar = True` para que o migration_agent refaça a
      migração — ao invés de forçar uma aprovação sem embasamento.

    Usa modelo Gemini: meta-avaliação da qualidade dos achados (raciocínio sobre raciocínio).
    """
    todos_achados = (
        state.get("achados_semantica", [])
        + state.get("achados_seguranca", [])
        + state.get("achados_lint", [])
    )

    # Early exit by severity: no LLM call needed.
    # If there are no [P0] or [P1] findings, all findings are cosmetic (P2/P3).
    # Additional iterations do not improve cosmetic findings — they only consume tokens.
    if not _tem_achado_critico(todos_achados):
        return {
            "status_qualidade": "approved",
            "motivo_rejeicao":  "",
            "deve_reprocessar": False,
        }

    llm = _get_llm("no_critico")

    achados_str = "\n".join([
        "### Semantics:",
        *state.get("achados_semantica", ["(not triggered)"]),
        "",
        "### Security:",
        *state.get("achados_seguranca", ["(not triggered)"]),
        "",
        "### Lint/Style:",
        *state.get("achados_lint", ["(not triggered)"]),
    ])

    prompt = _render(
        "no_critico",
        iteracao=str(state.get("iteracao", 0)),
        achados_str=achados_str,
    )
    response = _invoke_com_retry(llm, prompt)
    try:
        resultado = json.loads(_strip_md_fences(response.content))
        decisao = resultado.get("decision", "approved")
        motivo  = resultado.get("rejection_reason", "")
    except json.JSONDecodeError:
        decisao = "approved"
        motivo  = ""

    # Maximum iteration reached: the loop cannot continue.
    # If the LLM still rejects, signal deve_reprocessar to the migration_agent.
    if state.get("iteracao", 0) >= _MAX_ITERACOES and decisao == "requires_refinement":
        return {
            "status_qualidade":  "approved",   # force loop exit
            "motivo_rejeicao":   motivo,
            "deve_reprocessar":  True,
        }

    return {
        "status_qualidade": decisao,
        "motivo_rejeicao":  motivo,
        "deve_reprocessar": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Edge Condicional do Nó Crítico
# ─────────────────────────────────────────────────────────────────────────────

def _rota_pos_critico(
    state: CodeReviewState,
) -> Literal["relatorio_final", "no_roteador"]:
    """
    - "aprovado" ou iteração máxima atingida → gera relatório final.
    - "requer_refinamento"                   → volta ao Roteador para nova rodada.
    """
    if (
        state.get("status_qualidade") == "approved"
        or state.get("iteracao", 0) >= _MAX_ITERACOES
    ):
        return "relatorio_final"
    return "no_roteador"


# ─────────────────────────────────────────────────────────────────────────────
# Nó 6 – Relatório Final
# ─────────────────────────────────────────────────────────────────────────────

def no_relatorio_final(state: CodeReviewState) -> dict:
    """
    Consolida todos os achados validados em um relatório Markdown estruturado.
    A IA NÃO corrige o código — apenas reporta e orienta.

    O relatório segue um template rígido definido em relatorio_final.json com
    seções obrigatórias: Summary, Findings by Severity, Details, Verdict.
    Achados do histórico de iterações são incluídos quando há mais de 1 rodada.

    Quando `deve_reprocessar = True`, antecipa um cabeçalho de alerta indicando
    que o migration_agent deve ser acionado novamente.

    Usa modelo 8B: consolida achados já estruturados em Markdown.
    """
    llm = _get_llm("relatorio_final")

    achados_str = "\n".join([
        "### Semantics:",
        *state.get("achados_semantica", ["(not triggered)"]),
        "",
        "### Security:",
        *state.get("achados_seguranca", ["(not triggered)"]),
        "",
        "### Lint/Style:",
        *state.get("achados_lint", ["(not triggered)"]),
    ])
    diff_str      = json.dumps(state.get("diff_estruturado", {}), ensure_ascii=False, indent=2)
    historico     = state.get("historico_achados", [])
    hist_str      = json.dumps(historico, ensure_ascii=False, indent=2) if historico else "[]"
    total_iters   = str(state.get("iteracao", 1))
    deve_reprocess = state.get("deve_reprocessar", False)
    verdict_hint  = (
        "FORCED VERDICT: ❌ REQUIRES CORRECTIONS — the reflection loop exhausted 3 iterations "
        "and still found unresolved issues. The migration_agent must redo the migration."
        if deve_reprocess
        else "Determine the verdict based on the findings above."
    )

    prompt = _render(
        "relatorio_final",
        achados_str=achados_str,
        diff_str=diff_str,
        historico_str=hist_str,
        total_iteracoes=total_iters,
        verdict_hint=verdict_hint,
    )
    response = _invoke_com_retry(llm, prompt)

    conteudo = response.content
    if state.get("deve_reprocessar"):
        aviso = (
            "# ⚠️ ACTION REQUIRED: Reprocessing by Migration Agent\n\n"
            "> The critical node evaluated **3 refinement iterations** and still identified "
            "critical issues. The migration must be redone by the **migration_agent** "
            "before a new review.\n\n---\n\n"
        )
        conteudo = aviso + conteudo

    return {"relatorio_final": conteudo}


# ─────────────────────────────────────────────────────────────────────────────
# Montagem do Grafo
# ─────────────────────────────────────────────────────────────────────────────

def _build_graph() -> Any:
    workflow = StateGraph(CodeReviewState)

    # Registrar nós
    workflow.add_node("no_parser",        no_parser)
    workflow.add_node("no_classificador", no_classificador)
    workflow.add_node("no_roteador",      no_roteador)
    workflow.add_node("no_semantico",     no_semantico)
    workflow.add_node("no_seguranca",     no_seguranca)
    workflow.add_node("no_lint",          no_lint)
    workflow.add_node("no_critico",       no_critico)
    workflow.add_node("relatorio_final",  no_relatorio_final)

    # Fluxo linear de entrada
    workflow.add_edge(START,              "no_parser")
    workflow.add_edge("no_parser",        "no_classificador")
    workflow.add_edge("no_classificador", "no_roteador")

    # Fan-out paralelo via Send: no_roteador → no_semantico / no_seguranca / no_lint
    workflow.add_conditional_edges("no_roteador", _despachar_agentes)

    # Convergência: todos os nós especialistas apontam para o no_critico
    workflow.add_edge("no_semantico", "no_critico")
    workflow.add_edge("no_seguranca", "no_critico")
    workflow.add_edge("no_lint",      "no_critico")

    # Reflection loop: Nó Crítico → aprovado/encerrar OU requer_refinamento/retry
    workflow.add_conditional_edges(
        "no_critico",
        _rota_pos_critico,
        {
            "relatorio_final": "relatorio_final",
            "no_roteador":     "no_roteador",
        },
    )

    workflow.add_edge("relatorio_final", END)

    return workflow.compile()


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────────────────────────────────────

class CodeReviewRequest(BaseModel):
    codigo_original: str
    codigo_migrado: str


graph = _build_graph()

app = FastAPI(title="CodeReviewAgent", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _executar_grafo(codigo_original: str, codigo_migrado: str) -> dict:
    """Inicializa o estado e executa o grafo. Compartilhado pelos dois endpoints."""
    initial_state: CodeReviewState = {
        "codigo_original":   codigo_original,
        "codigo_migrado":    codigo_migrado,
        "raw_diff":          "",
        "diff_estruturado":  {},
        "agentes_acionados": [],
        "achados_semantica": [],
        "achados_seguranca": [],
        "achados_lint":      [],
        "motivo_rejeicao":   "",
        "status_qualidade":  "",
        "iteracao":          0,
        "deve_reprocessar":  False,
        "relatorio_final":   "",
        "ruff_config":       None,
        "ruff_novos_issues": None,
        "historico_achados": [],
    }
    result = graph.invoke(initial_state)
    return {
        "raw_diff":           result.get("raw_diff", ""),
        "diff":               result.get("diff_estruturado", {}),
        "agentes_acionados":  result.get("agentes_acionados", []),
        "achados_semantica":  result.get("achados_semantica", []),
        "achados_seguranca":  result.get("achados_seguranca", []),
        "achados_lint":       result.get("achados_lint", []),
        "iteracoes":          result.get("iteracao", 0),
        "historico_achados":  result.get("historico_achados", []),
        "deve_reprocessar":   result.get("deve_reprocessar", False),
        "relatorio_final":    result.get("relatorio_final", ""),
    }


@app.post("/review", summary="Revisão via JSON (strings)")
def review_code(request: CodeReviewRequest):
    """
    Recebe os dois trechos de código como strings JSON.
    Ideal para integração programática.
    """
    return _executar_grafo(request.codigo_original, request.codigo_migrado)


@app.post("/review/files", summary="Revisão via upload de arquivos .py")
async def review_code_files(
    codigo_original: UploadFile,
    codigo_migrado: UploadFile,
):
    """
    Recebe os dois arquivos **.py** diretamente via multipart/form-data.
    Ideal para testes no Swagger UI: basta clicar em **Choose File** e selecionar
    os arquivos sem se preocupar com escaping de JSON.
    """
    original = (await codigo_original.read()).decode("utf-8")
    migrado  = (await codigo_migrado.read()).decode("utf-8")
    return _executar_grafo(original, migrado)
