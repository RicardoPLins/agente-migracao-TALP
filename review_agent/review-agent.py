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

import ast
import json
import os
import re
import subprocess
import sys
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
# Priority 2 — Cloud fallback (when Ollama is not running), four tiers:
#   Tier 1 — Gemini 2.5 Pro    → no_semantico, no_critico (deep reasoning)
#   Tier 2 — Gemini 2.5 Flash  → no_parser, no_seguranca (structured analysis)
#   Tier 3 — Groq llama-3.3-70b → no_lint (technical interpretation of tool output)
#   Tier 4 — Groq llama-3.1-8b  → no_classificador, relatorio_final (mechanical tasks)
#
# Detection runs once at startup; no overhead per node call.

_MODEL_GROQ_LIGHT   = os.getenv("REVIEW_GROQ_MODEL_LIGHT", "llama-3.1-8b-instant")
_MODEL_GROQ_LINT     = os.getenv("REVIEW_GROQ_MODEL_LINT", "llama-3.3-70b-versatile")
_MODEL_GEMINI_FLASH = os.getenv("REVIEW_GEMINI_MODEL_HEAVY", "gemini-2.5-flash")
_MODEL_GEMINI_PRO    = os.getenv("REVIEW_GEMINI_MODEL_REASONING", "gemini-2.5-pro")
_OLLAMA_HOST         = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Two-tier Ollama strategy: heavy nodes get a larger/code-specific model,
# light nodes get a smaller/faster model.
# Override via env vars:
#   REVIEW_OLLAMA_MODEL_HEAVY=qwen2.5-coder:14b  (parser, semantic, security, critic)
#   REVIEW_OLLAMA_MODEL_LIGHT=qwen2.5-coder:3b   (classifier, lint, report)
# Single-model fallback: REVIEW_OLLAMA_MODEL applies to all nodes.
_MODEL_OLLAMA_HEAVY = os.getenv(
    "REVIEW_OLLAMA_MODEL_HEAVY",
    os.getenv("REVIEW_OLLAMA_MODEL", "qwen2.5-coder:14b"),
)
_MODEL_OLLAMA_LIGHT = os.getenv(
    "REVIEW_OLLAMA_MODEL_LIGHT",
    os.getenv("REVIEW_OLLAMA_MODEL", "qwen2.5-coder:3b"),
)
# Legacy alias — still used by _detectar_ollama_local to verify availability
_MODEL_OLLAMA = _MODEL_OLLAMA_HEAVY

# Ollama routing — all analysis nodes use the heavy model.
_OLLAMA_HEAVY_NODES: frozenset[str] = frozenset({
    "no_parser",
    "no_semantico",
    "no_seguranca",
    "no_critico",
})

# Cloud routing — tiered by task profile.
_REASONING_NODES: frozenset[str] = frozenset({
    "no_semantico",  # multi-step functional equivalence reasoning
    "no_critico",    # meta-evaluation of finding quality (reflection)
})

_FLASH_NODES: frozenset[str] = frozenset({
    "no_parser",     # structured JSON extraction from long diffs
    "no_seguranca",  # security domain analysis
})

_LIGHT_NODES: frozenset[str] = frozenset({
    "no_classificador",  # diff pattern matching → agent list
    "relatorio_final",   # consolidates already-structured findings into Markdown
})
# no_lint → handled separately (Groq 70b for technical tool-output interpretation)

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


def _resolver_modelo_ollama(
    desejado: str,
    disponiveis: list[str],
    *,
    papel: str = "heavy",
) -> str:
    """
    Resolves the best available Ollama model for a requested model name.
    Priority: exact match → prefix match → coder model (heavy) → warn + fallback.
    """
    if desejado in disponiveis:
        return desejado
    base = desejado.split(":")[0]
    match = next((m for m in disponiveis if m.startswith(base)), None)
    if match:
        if match != desejado:
            print(
                f"  [review_agent] AVISO: modelo '{desejado}' não instalado; "
                f"usando '{match}' ({papel})"
            )
        return match
    if papel == "heavy":
        coder = next((m for m in disponiveis if "coder" in m.lower()), None)
        if coder:
            print(
                f"  [review_agent] AVISO: '{desejado}' não encontrado; "
                f"usando modelo coder '{coder}' ({papel})"
            )
            return coder
    fallback = disponiveis[0]
    print(
        f"  [review_agent] AVISO: '{desejado}' não encontrado; "
        f"fallback '{fallback}' ({papel})"
    )
    return fallback


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

        heavy = _resolver_modelo_ollama(_MODEL_OLLAMA_HEAVY, modelos, papel="heavy")
        light = _resolver_modelo_ollama(_MODEL_OLLAMA_LIGHT, modelos, papel="light")
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
    print(
        "  [review_agent] Ollama not detected — cloud tiers: "
        "Gemini Pro (semantico/critico) + Gemini Flash (parser/seguranca) + "
        "Groq 70b (lint) + Groq 8b (classificador/relatorio)"
    )


def _get_llm(node: str = "default") -> ChatOllama | ChatGroq | ChatGoogleGenerativeAI:
    """
    Returns the appropriate LLM for the given node.

    When Ollama is running locally — two-tier strategy:
      Heavy nodes → REVIEW_OLLAMA_MODEL_HEAVY (default: qwen2.5-coder:14b)
        • no_parser, no_semantico, no_seguranca, no_critico
      Light nodes → REVIEW_OLLAMA_MODEL_LIGHT (default: qwen2.5-coder:3b)
        • no_classificador, no_lint, relatorio_final

    When Ollama is unavailable (cloud fallback), four tiers:
      Tier 1 — Gemini 2.5 Pro    → no_semantico, no_critico
      Tier 2 — Gemini 2.5 Flash  → no_parser, no_seguranca
      Tier 3 — Groq 70b          → no_lint
      Tier 4 — Groq 8b           → no_classificador, relatorio_final
    """
    if _OLLAMA_DISPONIVEL:
        ollama_model = (
            _OLLAMA_HEAVY_ATIVO if node in _OLLAMA_HEAVY_NODES else _OLLAMA_LIGHT_ATIVO
        )
        return ChatOllama(
            model=ollama_model,
            base_url=_OLLAMA_HOST,
            temperature=0.0,
        )

    google_key = os.getenv("GOOGLE_API_KEY")
    groq_key   = os.getenv("GROQ_API_KEY")

    if node in _REASONING_NODES:
        if not google_key:
            raise ValueError(
                "GOOGLE_API_KEY not found. Either start Ollama or set this env var."
            )
        return ChatGoogleGenerativeAI(
            model=_MODEL_GEMINI_PRO,
            temperature=0.0,
        )

    if node in _FLASH_NODES:
        if not google_key:
            raise ValueError(
                "GOOGLE_API_KEY not found. Either start Ollama or set this env var."
            )
        return ChatGoogleGenerativeAI(
            model=_MODEL_GEMINI_FLASH,
            temperature=0.0,
        )

    if not groq_key:
        raise ValueError(
            "GROQ_API_KEY not found. Either start Ollama or set this env var."
        )

    if node == "no_lint":
        return ChatGroq(
            model=_MODEL_GROQ_LINT,
            temperature=0.0,
        )

    return ChatGroq(
        model=_MODEL_GROQ_LIGHT,
        temperature=0.0,
    )


def _get_backend_label() -> str:
    """Descreve o backend LLM efetivamente em uso (para logs e JSON de saída)."""
    if _OLLAMA_DISPONIVEL:
        if _OLLAMA_HEAVY_ATIVO == _OLLAMA_LIGHT_ATIVO:
            return f"Ollama ({_OLLAMA_HEAVY_ATIVO})"
        return (
            f"Ollama (heavy={_OLLAMA_HEAVY_ATIVO}, light={_OLLAMA_LIGHT_ATIVO}; "
            f"semantico→{_OLLAMA_HEAVY_ATIVO})"
        )
    return "Gemini Pro/Flash + Groq 70b/8b (cloud tiers)"


def _invoke_com_retry(llm: Any, prompt: str, max_tentativas: int = 3) -> Any:
    """
    Invoca o LLM com retry exponencial em caso de rate limit (429/413).

    Este wrapper é a rede de segurança para erros de cota que escapam das
    estratégias primárias (Gemini Pro/Flash + Groq 70b/8b por tier de nó).
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
    mypy_findings:     list | None   # Erros mypy do código migrado (iter 1)
    lint_achados_pinados: list | None  # Achados confirmados iter 1 — propagados sem re-LLM

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


def _extrair_imports_top_level(code_str: str) -> set[str]:
    """Extrai nomes de módulo de nível superior via AST (determinístico)."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        return set()
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def _normalizar_deps_diff(
    codigo_original: str,
    codigo_migrado: str,
    diff: dict,
) -> dict:
    """
    Recalcula added/removed_dependencies a partir dos imports reais nos arquivos.
    Corrige alucinações do LLM (ex.: listar gzip/os como removidos quando ainda importados).
    """
    orig = _extrair_imports_top_level(codigo_original)
    mig = _extrair_imports_top_level(codigo_migrado)
    diff = dict(diff)
    diff["added_dependencies"] = sorted(mig - orig)
    diff["removed_dependencies"] = sorted(orig - mig)
    return diff


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

    if isinstance(diff, dict) and not diff.get("parse_error"):
        diff = _normalizar_deps_diff(
            state["codigo_original"],
            state["codigo_migrado"],
            diff,
        )

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

    Padrões de regressão comuns estão no prompt (exemplos generalizados) — o LLM
    infere a migração a partir do diff estruturado e do raw_diff.

    Usa Gemini 2.5 Pro (cloud) / Ollama heavy: raciocínio multi-step sobre equivalência funcional.
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
    achados = list(dict.fromkeys(
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ))
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

    Usa Gemini 2.5 Flash (cloud) / Ollama heavy: análise de domínio em segurança de software.
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
    achados = list(dict.fromkeys(
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ))
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
    defaults: dict[str, Any] = {"line_length": 88, "select": ["E", "W", "F", "I"], "ignore": [], "indent_width": 4}
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


def _run_mypy(code_str: str) -> list[dict]:
    """
    Executa mypy sobre o código migrado e retorna achados do tipo error/warning.
    Detecta chamadas de atributos inexistentes (attr-defined), erros de tipo, etc.
    Usa sys.executable para garantir o mypy correto do ambiente virtual ativo.
    Retorna [] em caso de falha (mypy não instalado, erro de sintaxe, etc.).
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(code_str)
            tmp_path = tmp.name

        result = subprocess.run(
            [
                sys.executable, "-m", "mypy", tmp_path,
                "--ignore-missing-imports",
                "--no-error-summary",
                "--no-color-output",
                "--show-error-codes",
                "--check-untyped-defs",   # analyse bodies of unannotated functions
                                          # (required to catch attr-defined inside methods)
            ],
            capture_output=True, text=True, timeout=60,
        )
        findings: list[dict] = []
        # mypy output: path:line: severity: message  [error-code]
        # Use greedy path match so Windows drive letters (C:\...) parse correctly.
        pattern = re.compile(
            r"^(.+):(\d+): (error|warning|note): (.+?)(?:\s+\[([^\]]+)\])?$"
        )
        for line in result.stdout.splitlines():
            m = pattern.match(line)
            if m and m.group(3) in ("error", "warning"):
                findings.append({
                    "line":     int(m.group(2)),
                    "severity": m.group(3),
                    "message":  m.group(4).strip(),
                    "code":     m.group(5) or "",
                })
        return findings
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _mypy_para_achados(mypy_findings: list[dict]) -> list[str]:
    """
    Converte saída mypy em achados formatados — determinístico, sem LLM.
    Regras fixas alinhadas ao error-code do mypy (única fonte determinística aqui).
    """
    achados: list[str] = []
    for f in mypy_findings:
        line = f.get("line", "?")
        msg = f.get("message", "")
        code = f.get("code", "")
        sev = f.get("severity", "error")
        if sev == "error" and code == "attr-defined":
            achados.append(
                f"- [BLOCKER][P0] line {line} — {msg} "
                f"Trigger: AttributeError at runtime when this line executes."
            )
        elif sev == "error" and code == "var-annotated":
            achados.append(
                f"- [TYPING-DRY][P2] line {line} — {msg} "
                f"Trigger: static type checker warning only; no runtime crash."
            )
        elif sev == "error":
            achados.append(
                f"- [WARNING][P1] line {line} — {msg} "
                f"Trigger: type error may cause runtime failure."
            )
        elif sev == "warning":
            achados.append(
                f"- [WARNING][P2] line {line} — {msg} "
                f"Trigger: type annotation regression."
            )
    return achados


_LINHA_ACHADO = re.compile(r"\bline\s+(\d+)\b", re.IGNORECASE)


def _filtrar_achados_linha_invalida(codigo: str, achados: list[str]) -> list[str]:
    """
    Descarta achados LLM cujo número de linha não existe no código migrado.
    Achados mypy já vêm com linhas validadas pelo compilador.
    """
    total = len(codigo.splitlines())
    validos: list[str] = []
    for achado in achados:
        m = _LINHA_ACHADO.search(achado)
        if not m:
            continue  # exige linha explícita
        linha = int(m.group(1))
        if 1 <= linha <= total:
            validos.append(achado)
    return validos


def no_lint(state: CodeReviewState) -> dict:
    """
    Valida lint e estilo do código migrado: Ruff + mypy (determinísticos) + LLM.

    Pipeline interno (iteração 1):
    1. LLM infere o estilo implícito do código original → config Ruff dinâmica.
    2. Ruff  — detecta issues de estilo/importação novos vs. original.
    3. mypy  — detecta atributos inexistentes, erros de tipo (convertidos sem LLM).
    4. LLM interpreta achados Ruff e verifica padrões de migração generalizados
       no código migrado (exemplos no prompt — não hardcoded no nó).
    5. Achados finais = mypy (determinístico) + LLM; salvos em `lint_achados_pinados`.

    Iterações 2+: `lint_achados_pinados` é retornado diretamente, sem re-invocar o LLM.
    """
    llm = _get_llm("no_lint")
    iteracao = state.get("iteracao", 1)

    if iteracao > 1 and state.get("lint_achados_pinados") is not None:
        return {
            "achados_lint":         state["lint_achados_pinados"],
            "ruff_config":          state.get("ruff_config"),
            "ruff_novos_issues":    state.get("ruff_novos_issues", []),
            "mypy_findings":        state.get("mypy_findings", []),
            "lint_achados_pinados": state["lint_achados_pinados"],
        }

    ruff_config     = _inferir_config_ruff(state["codigo_original"], llm)
    issues_original = _run_ruff(state["codigo_original"], ruff_config)
    issues_migrado  = _run_ruff(state["codigo_migrado"],  ruff_config)
    novos_issues    = _filtrar_novos_issues(issues_original, issues_migrado)
    mypy_findings   = _run_mypy(state["codigo_migrado"])
    mypy_achados    = _mypy_para_achados(mypy_findings)

    prompt = _render(
        "agente_lint_interpretacao",
        critica="",
        novos_issues=json.dumps(novos_issues, ensure_ascii=False, indent=2),
        estilo_inferido=json.dumps(ruff_config, ensure_ascii=False, indent=2),
        codigo_migrado=state["codigo_migrado"],
    )
    response = _invoke_com_retry(llm, prompt)
    llm_achados = list(dict.fromkeys(
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ))
    llm_achados = _filtrar_achados_linha_invalida(state["codigo_migrado"], llm_achados)

    achados_finais = list(dict.fromkeys(mypy_achados + llm_achados))
    if not achados_finais:
        achados_finais = ["- [INFO][P3] No relevant new lint/style issues identified."]

    return {
        "achados_lint":         achados_finais,
        "ruff_config":          ruff_config,
        "ruff_novos_issues":    novos_issues,
        "mypy_findings":        mypy_findings,
        "lint_achados_pinados": achados_finais,
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

    Usa Gemini 2.5 Pro (cloud) / Ollama heavy: meta-avaliação da qualidade dos achados.
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

def _achados_relevantes(achados: list[str]) -> list[str]:
    """Remove linhas 'no findings' dos achados finais."""
    return [
        a for a in achados
        if "[INFO]" not in a and "No relevant" not in a and "(not triggered)" not in a
    ]


def _agrupar_por_severidade(achados: list[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {px: [] for px in ("P0", "P1", "P2", "P3")}
    for achado in achados:
        for px in buckets:
            if f"[{px}]" in achado:
                buckets[px].append(achado)
                break
    return buckets


def _formatar_lista_achados(items: list[str]) -> str:
    return "\n".join(items) if items else "_(none)_"


def _montar_recomendacoes(buckets: dict[str, list[str]]) -> str:
    linhas: list[str] = []
    if buckets["P0"]:
        linhas.append(
            "1. Fix all P0 blockers first — these cause guaranteed runtime failures."
        )
    if buckets["P1"]:
        linhas.append(
            "2. Address P1 high-severity regressions before merging."
        )
    if buckets["P2"]:
        linhas.append(
            "3. Resolve P2 typing/style issues to keep maintainability."
        )
    if buckets["P3"]:
        linhas.append(
            "4. P3 cosmetic items are optional but improve code quality."
        )
    if not linhas:
        linhas.append("No actionable items — migration review passed.")
    return "\n".join(linhas)


def _montar_relatorio_markdown(state: CodeReviewState, exec_summary: str) -> str:
    """
    Monta seções 2–6 deterministicamente a partir do estado FINAL do grafo.
    O LLM só preenche o Executive Summary (2–3 frases).
    """
    sem = _achados_relevantes(state.get("achados_semantica", []))
    seg = _achados_relevantes(state.get("achados_seguranca", []))
    lint = _achados_relevantes(state.get("achados_lint", []))
    todos = sem + seg + lint
    buckets = _agrupar_por_severidade(todos)

    historico = state.get("historico_achados", [])
    hist_str = json.dumps(historico, ensure_ascii=False, indent=2) if historico else "[]"
    total_iters = state.get("iteracao", 1)
    deve_reprocess = state.get("deve_reprocessar", False)

    if deve_reprocess:
        verdict = (
            "❌ REQUIRES CORRECTIONS — the reflection loop exhausted 3 iterations "
            "and still found unresolved issues. The migration_agent must redo the migration."
        )
    elif buckets["P0"] or buckets["P1"]:
        verdict = "❌ REQUIRES CORRECTIONS — unresolved P0/P1 findings remain."
    else:
        verdict = "✅ APPROVED — no P0/P1 findings in the final iteration."

    return f"""# Code Migration Review Report

## 1. Executive Summary
{exec_summary}

---

## 2. Findings by Severity

### 🔴 Critical (P0)
{_formatar_lista_achados(buckets["P0"])}

### 🟠 High (P1)
{_formatar_lista_achados(buckets["P1"])}

### 🟡 Medium (P2)
{_formatar_lista_achados(buckets["P2"])}

### 🟢 Low / Cosmetic (P3)
{_formatar_lista_achados(buckets["P3"])}

---

## 3. Detailed Findings

### Semantic Findings
{_formatar_lista_achados(sem)}

### Security Findings
{_formatar_lista_achados(seg)}

### Lint / Style Findings
{_formatar_lista_achados(lint)}

---

## 4. Reflection Loop History
Total iterations: {total_iters}

{hist_str}

---

## 5. Priority Recommendations
{_montar_recomendacoes(buckets)}

---

## 6. Final Verdict
{verdict}
"""


def no_relatorio_final(state: CodeReviewState) -> dict:
    """
    Consolida achados validados em relatório Markdown.

    Seções 2–6 são montadas deterministicamente a partir do estado FINAL
    (achados_semantica/seguranca/lint da última iteração) — o histórico
    aparece apenas na seção 4 e não contamina findings descartados.

    O LLM gera apenas o Executive Summary (2–3 frases).
    """
    llm = _get_llm("relatorio_final")

    sem = _achados_relevantes(state.get("achados_semantica", []))
    seg = _achados_relevantes(state.get("achados_seguranca", []))
    lint = _achados_relevantes(state.get("achados_lint", []))
    achados_resumo = "\n".join([
        "### Semantics:",
        *(sem if sem else ["_(none)_"]),
        "",
        "### Security:",
        *(seg if seg else ["_(none)_"]),
        "",
        "### Lint/Style:",
        *(lint if lint else ["_(none)_"]),
    ])

    prompt = _render(
        "relatorio_final",
        achados_resumo=achados_resumo,
        deve_reprocessar=str(state.get("deve_reprocessar", False)),
    )
    response = _invoke_com_retry(llm, prompt)
    exec_summary = response.content.strip() or (
        "Migration review completed. See findings by severity below."
    )

    conteudo = _montar_relatorio_markdown(state, exec_summary)

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
        "ruff_config":          None,
        "ruff_novos_issues":    None,
        "mypy_findings":        None,
        "lint_achados_pinados": None,
        "historico_achados":    [],
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
        "backend":            _get_backend_label(),
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
