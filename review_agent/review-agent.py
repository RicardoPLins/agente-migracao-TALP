"""
CodeReviewAgent – Orquestração LangGraph para revisão de migração

Fluxo (passagem única por execução do review):
  Entrada → [no_parser] → [no_classificador] → [no_roteador] ──fan-out──►
  [no_semantico]  ─┐
  [no_seguranca]   ├──► [no_critico] → [relatorio_final] → END
  [no_lint/Ruff]   ┘

no_critico (determinístico):
  · P0/P1 → deve_reprocessar=True + feedback_migracao (P0–P3) → orquestrador externo
  · só P2/P3 → deve_reprocessar=False → relatório para correção humana

Loop migration → test → review (até 3×) fica no test_pipeline.py.
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
from typing import Any, TypedDict

from fastapi import FastAPI, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from langchain_groq import ChatGroq
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
# Priority 2 — Cloud fallback (when Ollama is not running), two tiers:
#   Heavy — Groq llama-3.3-70b-versatile → no_semantico, no_seguranca, no_lint
#   Light — Groq llama-3.1-8b-instant     → no_classificador, relatorio_final
#   no_parser, no_critico → git+AST / determinístico (sem LLM)
#
# Override via env:
#   REVIEW_GROQ_MODEL_HEAVY=llama-3.3-70b-versatile
#   REVIEW_GROQ_MODEL_LIGHT=llama-3.1-8b-instant
#
# Detection runs once at startup; no overhead per node call.

_MODEL_GROQ_HEAVY   = os.getenv("REVIEW_GROQ_MODEL_HEAVY", "llama-3.3-70b-versatile")
_MODEL_GROQ_LIGHT   = os.getenv("REVIEW_GROQ_MODEL_LIGHT", "llama-3.1-8b-instant")
_MODEL_GROQ_LINT    = os.getenv("REVIEW_GROQ_MODEL_LINT", _MODEL_GROQ_HEAVY)
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

# Ollama routing — analysis nodes (no_parser/no_critico/no_classificador sem LLM).
_OLLAMA_HEAVY_NODES: frozenset[str] = frozenset({
    "no_semantico",
    "no_seguranca",
})

# Cloud routing — Groq heavy vs light.
_HEAVY_CLOUD_NODES: frozenset[str] = frozenset({
    "no_semantico",
    "no_seguranca",
    "no_lint",
})

_LIGHT_NODES: frozenset[str] = frozenset({
    "no_classificador",  # escolhe agentes a partir do diff estruturado (Groq 8b cloud)
    "relatorio_final",
})
# no_parser, no_critico → determinísticos, sem LLM
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


_AGENTES_TODOS = ["semantics", "security", "lint"]

_SEC_IMPACT_KW = (
    "authentication", "password", "token", "key", "encryption", "hash", "sign", "verify",
    "http", "request", "network", "socket", "file", "sensitive", "credential",
    "permission", "authorization", "session", "cookie", "jwt", "oauth", "https", "tls", "ssl",
)
_SEC_NAME_KW = (
    "auth", "login", "logout", "sign", "verify", "token", "credential", "secret",
    "password", "passwd", "api_key", "apikey", "bearer", "oauth", "jwt", "session",
    "permission", "grant", "revoke", "log", "logger", "logging", "audit", "track",
    "record", "monitor", "debug", "trace", "report", "validate", "validation",
    "sanitize", "sanitise", "parse_input", "check_input", "guard", "filter", "escape",
    "encode", "decode", "clean", "verify_input", "assert_valid", "request", "fetch",
    "connect", "call", "send", "post", "get", "put", "delete", "patch", "upload",
    "download", "read_file", "write_file", "open_file", "execute", "run_command",
)
_SEM_IMPACT_KW = ("behavior", "logic", "return", "contract", "comportamento", "lógica", "api")


def _classificar_agentes_por_diff(diff: dict) -> list[str]:
    """Regras do classificador.json — determinístico, sem LLM."""
    if diff.get("parse_error"):
        return list(_AGENTES_TODOS)

    altered_fn = diff.get("altered_functions") or []
    added_fn = diff.get("added_functions") or []
    removed_fn = diff.get("removed_functions") or []
    altered_cls = diff.get("altered_classes") or []
    added_deps = diff.get("added_dependencies") or []
    removed_deps = diff.get("removed_dependencies") or []
    impact = (diff.get("impact_summary") or "").lower()
    all_names = " ".join(str(x) for x in altered_fn + added_fn).lower()

    semantics = bool(altered_fn or added_fn or removed_fn or altered_cls)
    if not semantics and any(k in impact for k in _SEM_IMPACT_KW):
        semantics = True

    security = bool(added_deps or removed_deps)
    if not security and any(k in impact for k in _SEC_IMPACT_KW):
        security = True
    if not security and any(k in all_names for k in _SEC_NAME_KW):
        security = True

    lint = bool(
        altered_fn or added_fn or removed_fn or altered_cls or added_deps or removed_deps
    )

    agentes: list[str] = []
    if semantics:
        agentes.append("semantics")
    if security:
        agentes.append("security")
    if lint:
        agentes.append("lint")
    return agentes or list(_AGENTES_TODOS)


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
        "  [review_agent] Ollama not detected — cloud: "
        f"Groq 70b ({_MODEL_GROQ_HEAVY}) → semantico/seguranca/lint; "
        f"Groq 8b ({_MODEL_GROQ_LIGHT}) → classificador/relatorio; "
        "no_parser/no_critico → git+AST / determinístico"
    )


def _get_llm(node: str = "default") -> ChatOllama | ChatGroq:
    """
    Returns the appropriate LLM for the given node.

    Ollama (local):
      Heavy → REVIEW_OLLAMA_MODEL_HEAVY  (no_semantico, no_seguranca)
      Light → REVIEW_OLLAMA_MODEL_LIGHT  (no_classificador, no_lint, relatorio_final)
      no_parser, no_critico → sem LLM

    Cloud (sem Ollama) — requer GROQ_API_KEY:
      Heavy → llama-3.3-70b-versatile  (no_semantico, no_seguranca, no_lint)
      Light → llama-3.1-8b-instant     (no_classificador, relatorio_final)
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

    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        raise ValueError(
            "GROQ_API_KEY not found. Either start Ollama or set this env var."
        )

    if node in _HEAVY_CLOUD_NODES:
        model = _MODEL_GROQ_LINT if node == "no_lint" else _MODEL_GROQ_HEAVY
        return ChatGroq(model=model, temperature=0.0)

    return ChatGroq(model=_MODEL_GROQ_LIGHT, temperature=0.0)


def _get_backend_label() -> str:
    """Descreve o backend LLM efetivamente em uso (para logs e JSON de saída)."""
    if _OLLAMA_DISPONIVEL:
        if _OLLAMA_HEAVY_ATIVO == _OLLAMA_LIGHT_ATIVO:
            return f"Ollama ({_OLLAMA_HEAVY_ATIVO})"
        return (
            f"Ollama (heavy={_OLLAMA_HEAVY_ATIVO}, light={_OLLAMA_LIGHT_ATIVO}; "
            f"semantico→{_OLLAMA_HEAVY_ATIVO})"
        )
    return f"Groq 70b ({_MODEL_GROQ_HEAVY}) + Groq 8b ({_MODEL_GROQ_LIGHT}) (cloud)"


def _log_progress(tag: str, msg: str = "") -> None:
    """Log de progresso com timestamp — flush imediato para diagnóstico."""
    ts = time.strftime("%H:%M:%S")
    line = f"  [{ts}] [review] {tag}"
    if msg:
        line += f" — {msg}"
    print(line, flush=True)


def _llm_model_label(llm: Any) -> str:
    return getattr(llm, "model_name", None) or getattr(llm, "model", "?") or "?"


def _parse_json_resposta_llm(text: str) -> dict | None:
    """Extrai objeto JSON da resposta do LLM (com ou sem markdown fences)."""
    cleaned = _strip_md_fences(text)
    candidatos = [cleaned]
    if cleaned.strip().startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            candidatos.append(cleaned[start : end + 1])
    for cand in candidatos:
        if not cand:
            continue
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _normalizar_agentes_classificador(agentes: Any) -> list[str]:
    if not isinstance(agentes, list):
        return list(_AGENTES_TODOS)
    validos = [a for a in agentes if a in _AGENTES_TODOS]
    ordem = {nome: i for i, nome in enumerate(_AGENTES_TODOS)}
    return sorted(validos, key=lambda x: ordem[x]) if validos else list(_AGENTES_TODOS)


def _invoke_com_retry(
    llm: Any,
    prompt: str,
    max_tentativas: int = 3,
    *,
    step: str = "",
) -> Any:
    """
    Invoca o LLM com retry exponencial em caso de rate limit (429/413).
    Backoff: 30s → 60s → 120s (dobrando a cada tentativa).
    """
    label = step or "llm"
    model = _llm_model_label(llm)
    _log_progress(label, f"LLM iniciando ({model})")
    t0 = time.time()
    for tentativa in range(1, max_tentativas + 1):
        try:
            response = llm.invoke(prompt)
            _log_progress(label, f"LLM concluído em {time.time() - t0:.1f}s")
            return response
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
                wait = 30 * (2 ** (tentativa - 1))
                _log_progress(
                    label,
                    f"rate limit — tentativa {tentativa}/{max_tentativas}, aguardando {wait}s",
                )
                time.sleep(wait)
            else:
                _log_progress(label, f"LLM falhou após {time.time() - t0:.1f}s — {err[:120]}")
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

    # Feedback do Nó Crítico — enviado ao migration_agent quando deve_reprocessar
    motivo_rejeicao: str
    feedback_migracao: str

    # Controle de qualidade
    status_qualidade: str   # "" | "approved"
    iteracao: int           # 1 por execução (single pass)
    deve_reprocessar: bool  # True = P0/P1 → orquestrador refaz migration→test→review

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
            diff_text = result.stdout.strip() or "(sem diferenças entre os arquivos)"
            n_linhas = diff_text.count("\n") + 1
            _log_progress(
                "no_parser",
                f"raw_diff: git — {n_linhas} linhas, {len(diff_text)} chars "
                f"(exit={result.returncode})",
            )
            return diff_text

        stderr = (result.stderr or "").strip().replace("\n", " ")[:120]
        _log_progress(
            "no_parser",
            f"raw_diff: git falhou (exit={result.returncode})"
            + (f" — {stderr}" if stderr else ""),
        )
        return None

    except FileNotFoundError:
        _log_progress("no_parser", "raw_diff: git não encontrado no PATH")
        return None
    except subprocess.TimeoutExpired:
        _log_progress("no_parser", "raw_diff: git diff timeout (15s)")
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
    """Recalcula added/removed_dependencies a partir dos imports reais nos arquivos."""
    orig = _extrair_imports_top_level(codigo_original)
    mig = _extrair_imports_top_level(codigo_migrado)
    diff = dict(diff)
    diff["added_dependencies"] = sorted(mig - orig)
    diff["removed_dependencies"] = sorted(orig - mig)
    return diff


def _fingerprint_ast(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _extrair_simbolos_ast(code_str: str) -> tuple[dict[str, str], dict[str, str]] | None:
    """Extrai funções/métodos e classes com fingerprint AST. None se SyntaxError."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        return None

    functions: dict[str, str] = {}
    classes: dict[str, str] = {}

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = _fingerprint_ast(node)
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = _fingerprint_ast(node)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[item.name] = _fingerprint_ast(item)

    return functions, classes


def _montar_impact_summary_ast(diff: dict) -> str:
    partes: list[str] = []
    added_deps = diff.get("added_dependencies") or []
    removed_deps = diff.get("removed_dependencies") or []
    if added_deps or removed_deps:
        partes.append(
            f"Dependency migration: removed {removed_deps or []}, added {added_deps or []}."
        )

    altered_fn = diff.get("altered_functions") or []
    added_fn = diff.get("added_functions") or []
    removed_fn = diff.get("removed_functions") or []
    if altered_fn or added_fn or removed_fn:
        partes.append(
            "Function/method changes: "
            f"altered={altered_fn}, added={added_fn}, removed={removed_fn}."
        )

    altered_cls = diff.get("altered_classes") or []
    if altered_cls:
        partes.append(f"Class structure changes: {altered_cls}.")

    if not partes:
        return "No structural differences detected between original and migrated code."

    if altered_fn or added_fn or removed_fn or added_deps or removed_deps:
        partes.append("Behavior, logic, and API contract may have changed due to migration.")
    return " ".join(partes)


def _ast_diff(codigo_original: str, codigo_migrado: str) -> dict:
    """Diff estruturado determinístico via AST + imports (sem LLM)."""
    orig = _extrair_simbolos_ast(codigo_original)
    mig = _extrair_simbolos_ast(codigo_migrado)

    if orig is None or mig is None:
        diff = {
            "altered_functions": [],
            "added_functions": [],
            "removed_functions": [],
            "altered_classes": [],
            "added_dependencies": [],
            "removed_dependencies": [],
            "impact_summary": "Syntax error in original or migrated code — AST diff unavailable.",
            "parse_error": True,
        }
        return _normalizar_deps_diff(codigo_original, codigo_migrado, diff)

    orig_fn, orig_cls = orig
    mig_fn, mig_cls = mig

    orig_fn_names = set(orig_fn)
    mig_fn_names = set(mig_fn)
    orig_cls_names = set(orig_cls)
    mig_cls_names = set(mig_cls)

    added_functions = sorted(mig_fn_names - orig_fn_names)
    removed_functions = sorted(orig_fn_names - mig_fn_names)
    altered_functions = sorted(
        name for name in orig_fn_names & mig_fn_names if orig_fn[name] != mig_fn[name]
    )

    added_classes = sorted(mig_cls_names - orig_cls_names)
    removed_classes = sorted(orig_cls_names - mig_cls_names)
    altered_existing_classes = sorted(
        name for name in orig_cls_names & mig_cls_names if orig_cls[name] != mig_cls[name]
    )
    altered_classes = sorted(set(altered_existing_classes) | set(added_classes))

    diff = {
        "altered_functions": altered_functions,
        "added_functions": added_functions,
        "removed_functions": removed_functions,
        "altered_classes": altered_classes,
        "added_dependencies": [],
        "removed_dependencies": [],
        "impact_summary": "",
    }
    diff = _normalizar_deps_diff(codigo_original, codigo_migrado, diff)
    diff["impact_summary"] = _montar_impact_summary_ast(diff)
    if removed_classes:
        diff["impact_summary"] += f" Removed classes: {removed_classes}."
    return diff


def no_parser(state: CodeReviewState) -> dict:
    """
    Compara código original e migrado (sem LLM).

    1. `git diff --no-index` → raw_diff (números de linha para os agentes).
    2. AST → diff_estruturado (funções/classes/deps alteradas).
    """
    _log_progress("no_parser", "início")
    raw_diff = _run_git_diff(state["codigo_original"], state["codigo_migrado"])
    raw_diff_fonte = "git" if raw_diff else "indisponivel"

    if not raw_diff:
        _log_progress(
            "no_parser",
            "raw_diff: indisponível — agentes sem diff unificado do git",
        )

    diff = _ast_diff(state["codigo_original"], state["codigo_migrado"])

    if diff.get("parse_error"):
        _log_progress("no_parser", "diff_estruturado: AST — falhou (SyntaxError no código)")
    else:
        _log_progress(
            "no_parser",
            f"diff_estruturado: AST — "
            f"altered_functions={len(diff.get('altered_functions', []))}, "
            f"added_functions={len(diff.get('added_functions', []))}, "
            f"removed_functions={len(diff.get('removed_functions', []))}, "
            f"altered_classes={len(diff.get('altered_classes', []))}, "
            f"deps +{diff.get('added_dependencies', [])} -{diff.get('removed_dependencies', [])}",
        )

    _log_progress(
        "no_parser",
        f"fim — raw_diff={raw_diff_fonte}, diff_estruturado=ast",
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
    LLM analisa diff estruturado + deps e decide quais agentes acionar.
    Fallback determinístico se o parse JSON falhar.
    """
    _log_progress("no_classificador", "início")
    diff = state["diff_estruturado"]

    if diff.get("parse_error") and not state.get("raw_diff"):
        raise RuntimeError(
            "AST diff unavailable and git diff is missing. "
            "Ensure git is installed and the input code is valid Python."
        )

    if diff.get("parse_error"):
        agentes = list(_AGENTES_TODOS)
        _log_progress(
            "no_classificador",
            f"fim — agentes: {agentes} (fallback AST parse_error → todos)",
        )
        return {"agentes_acionados": agentes}

    diff_str = json.dumps(diff, ensure_ascii=False, indent=2)
    llm = _get_llm("no_classificador")
    prompt = _render("classificador", diff_str=diff_str)
    response = _invoke_com_retry(llm, prompt, step="no_classificador")

    parsed = _parse_json_resposta_llm(response.content)
    if parsed and "agents" in parsed:
        agentes = _normalizar_agentes_classificador(parsed["agents"])
        _log_progress("no_classificador", f"fim — agentes: {agentes} (LLM)")
        return {"agentes_acionados": agentes}

    agentes = _classificar_agentes_por_diff(diff)
    preview = (response.content or "")[:120].replace("\n", " ")
    _log_progress(
        "no_classificador",
        f"parse JSON falhou — fallback determinístico: {agentes} | preview: {preview}",
    )
    return {"agentes_acionados": agentes}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 3 – Roteador (preparação de iteração + fan-out via Send)
# ─────────────────────────────────────────────────────────────────────────────

def no_roteador(state: CodeReviewState) -> dict:
    """Prepara fan-out dos agentes especialistas (passagem única, iteracao=1)."""
    agentes = state.get("agentes_acionados", [])
    _log_progress(
        "no_roteador",
        f"fan-out paralelo → {agentes if agentes else ['(direto ao crítico)']}",
    )
    return {
        "iteracao":          1,
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
    if envios:
        nos = [mapa_agentes[a] for a in state.get("agentes_acionados", []) if a in mapa_agentes]
        _log_progress("no_roteador", f"despachando {len(envios)} nó(s) em paralelo: {', '.join(nos)}")
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
    _log_progress("no_semantico", "início — equivalência semântica")
    diff_str = json.dumps(state["diff_estruturado"], ensure_ascii=False, indent=2)

    prompt = _render(
        "agente_semantica",
        critica="",
        diff_str=diff_str,
        raw_diff=state.get("raw_diff") or "(git diff not available)",
    )
    response = _invoke_com_retry(llm, prompt, step="no_semantico")
    achados = list(dict.fromkeys(
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ))
    achados = achados or ["- [INFO][P3] No relevant semantic findings."]
    _log_progress("no_semantico", f"fim — {len(achados)} achado(s)")
    return {"achados_semantica": achados}


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
    _log_progress("no_seguranca", "início — auditoria de segurança")
    diff_str = json.dumps(state["diff_estruturado"], ensure_ascii=False, indent=2)

    prompt = _render(
        "agente_seguranca",
        critica="",
        diff_str=diff_str,
        raw_diff=state.get("raw_diff") or "(git diff not available)",
    )
    response = _invoke_com_retry(llm, prompt, step="no_seguranca")
    achados = list(dict.fromkeys(
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ))
    achados = achados or ["- [INFO][P3] No relevant security findings."]
    _log_progress("no_seguranca", f"fim — {len(achados)} achado(s)")
    return {"achados_seguranca": achados}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 4c – no_lint  (tool use determinístico via Ruff + interpretação LLM)
# ─────────────────────────────────────────────────────────────────────────────

# Rules that must always be active regardless of what the LLM infers from the original code.
# F401 — unused imports (catches leftover imports after migration removes the usage)
# B006 — mutable default arguments (common migration mistake)
# S603/S605/S607 — subprocess/os.system with dynamic arguments (shell injection)
_RUFF_MANDATORY_RULES: frozenset[str] = frozenset({"F401", "B006", "S603", "S605", "S607"})


def _inferir_config_ruff(codigo_original: str, llm: ChatOllama | ChatGroq) -> dict:
    """
    Usa o LLM para inferir o estilo implícito do código original e retorna
    uma configuração Ruff ajustada (line_length, regras, indent_width).

    O conjunto de regras retornado pelo LLM é complementado com
    _RUFF_MANDATORY_RULES que nunca podem ser omitidas, independente do estilo
    inferido (ex: F401 para imports órfãos, S605 para injeção via os.system).
    """
    prompt = _render("agente_lint_config", codigo_original=codigo_original)
    response = _invoke_com_retry(llm, prompt, step="no_lint/ruff_config")
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
    _log_progress("no_lint", "início — Ruff + mypy + interpretação LLM")

    if iteracao > 1 and state.get("lint_achados_pinados") is not None:
        _log_progress("no_lint", "reutilizando achados pinados (sem re-LLM)")
        return {
            "achados_lint":         state["lint_achados_pinados"],
            "ruff_config":          state.get("ruff_config"),
            "ruff_novos_issues":    state.get("ruff_novos_issues", []),
            "mypy_findings":        state.get("mypy_findings", []),
            "lint_achados_pinados": state["lint_achados_pinados"],
        }

    _log_progress("no_lint", "inferindo config Ruff a partir do original...")
    ruff_config     = _inferir_config_ruff(state["codigo_original"], llm)
    _log_progress("no_lint", "executando Ruff (original vs migrado)...")
    issues_original = _run_ruff(state["codigo_original"], ruff_config)
    issues_migrado  = _run_ruff(state["codigo_migrado"],  ruff_config)
    novos_issues    = _filtrar_novos_issues(issues_original, issues_migrado)
    _log_progress(
        "no_lint",
        f"Ruff — original={len(issues_original)} migrado={len(issues_migrado)} "
        f"novos={len(novos_issues)}",
    )
    _log_progress("no_lint", "executando mypy no migrado...")
    mypy_findings   = _run_mypy(state["codigo_migrado"])
    mypy_achados    = _mypy_para_achados(mypy_findings)
    _log_progress("no_lint", f"mypy — {len(mypy_findings)} finding(s), {len(mypy_achados)} achado(s)")

    prompt = _render(
        "agente_lint_interpretacao",
        critica="",
        novos_issues=json.dumps(novos_issues, ensure_ascii=False, indent=2),
        estilo_inferido=json.dumps(ruff_config, ensure_ascii=False, indent=2),
        codigo_migrado=state["codigo_migrado"],
    )
    response = _invoke_com_retry(llm, prompt, step="no_lint")
    llm_achados = list(dict.fromkeys(
        linha.strip()
        for linha in response.content.split("\n")
        if _PADRAO_ACHADO.match(linha.strip())
    ))
    llm_achados = _filtrar_achados_linha_invalida(state["codigo_migrado"], llm_achados)

    achados_finais = list(dict.fromkeys(mypy_achados + llm_achados))
    if not achados_finais:
        achados_finais = ["- [INFO][P3] No relevant new lint/style issues identified."]

    _log_progress("no_lint", f"fim — {len(achados_finais)} achado(s) total")
    return {
        "achados_lint":         achados_finais,
        "ruff_config":          ruff_config,
        "ruff_novos_issues":    novos_issues,
        "mypy_findings":        mypy_findings,
        "lint_achados_pinados": achados_finais,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Nó 5 – Nó Crítico (convergência + sinal ao orquestrador)
# ─────────────────────────────────────────────────────────────────────────────

_ALTA_SEVERIDADE = ("[P0]", "[P1]")
_SEVERIDADES_FEEDBACK = ("[P0]", "[P1]", "[P2]", "[P3]")


def _tem_achado_critico(achados: list[str]) -> bool:
    return any(sev in linha for linha in achados for sev in _ALTA_SEVERIDADE)


def _filtrar_achados_alta_severidade(achados: list[str]) -> list[str]:
    return [
        a for a in achados
        if any(sev in a for sev in _ALTA_SEVERIDADE)
        and "[INFO]" not in a
        and "No relevant" not in a
    ]


def _filtrar_achados_acionaveis(achados: list[str]) -> list[str]:
    return [
        a for a in achados
        if any(sev in a for sev in _SEVERIDADES_FEEDBACK)
        and "[INFO]" not in a
        and "No relevant" not in a
    ]


def _ordenar_por_severidade(achados: list[str]) -> list[str]:
    def _ordem(a: str) -> tuple[int, str]:
        for i, sev in enumerate(_SEVERIDADES_FEEDBACK):
            if sev in a:
                return (i, a)
        return (99, a)

    return sorted(achados, key=_ordem)


def _montar_feedback_migracao(
    achados_sem: list[str],
    achados_seg: list[str],
    achados_lint: list[str],
) -> str:
    """Texto para remigração: todos os achados P0–P3 quando há P0/P1."""
    linhas: list[str] = []
    for titulo, grupo in (
        ("Semântica", achados_sem),
        ("Segurança", achados_seg),
        ("Lint/Estilo", achados_lint),
    ):
        acionaveis = _ordenar_por_severidade(_filtrar_achados_acionaveis(grupo))
        if acionaveis:
            linhas.append(f"### {titulo}")
            linhas.extend(f"- {a.lstrip('- ').strip()}" for a in acionaveis)
            linhas.append("")
    if not linhas:
        return ""
    return (
        "CORREÇÕES DO REVIEW (P0/P1 bloqueiam o pipeline — refaça a migração "
        "corrigindo todos os itens abaixo, incluindo P2/P3 se listados):\n"
        + "\n".join(linhas).strip()
    )


def no_critico(state: CodeReviewState) -> dict:
    """
    Convergência dos agentes especialistas (determinístico, sem LLM).

    - Sem [P0]/[P1] → aprova; relatório final com P2/P3 para correção humana.
    - Com [P0]/[P1] → deve_reprocessar=True + feedback_migracao (P0–P3)
      para o test_pipeline reiniciar migration → test → review.
    """
    _log_progress("no_critico", "início")
    achados_sem = state.get("achados_semantica", [])
    achados_seg = state.get("achados_seguranca", [])
    achados_lint = state.get("achados_lint", [])
    todos_achados = achados_sem + achados_seg + achados_lint

    if not _tem_achado_critico(todos_achados):
        _log_progress("no_critico", "aprovado — sem P0/P1 (só P2/P3 ou nenhum)")
        return {
            "status_qualidade":  "approved",
            "motivo_rejeicao":   "",
            "feedback_migracao": "",
            "deve_reprocessar":  False,
        }

    feedback = _montar_feedback_migracao(achados_sem, achados_seg, achados_lint)
    n_p0p1 = len(_filtrar_achados_alta_severidade(todos_achados))
    n_total = len(_filtrar_achados_acionaveis(todos_achados))
    _log_progress(
        "no_critico",
        f"deve_reprocessar — {n_p0p1} P0/P1, {n_total} total (P0–P3) → migration_agent",
    )
    return {
        "status_qualidade":  "approved",
        "motivo_rejeicao":   feedback,
        "feedback_migracao": feedback,
        "deve_reprocessar":  True,
    }


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
            "❌ REQUIRES REMIGRATION — achados P0/P1 exigem novo ciclo "
            "migration → test → review (orquestrador, até 3 ciclos)."
        )
    elif buckets["P0"] or buckets["P1"]:
        verdict = "❌ REQUIRES CORRECTIONS — unresolved P0/P1 findings remain."
    elif buckets["P2"] or buckets["P3"]:
        verdict = (
            "✅ APPROVED for pipeline — no P0/P1 blockers. "
            "Review P2/P3 items below; a human should fix them before merge."
        )
    else:
        verdict = "✅ APPROVED — no findings requiring action."

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
    _log_progress("relatorio_final", "início — executive summary (LLM)")

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
    response = _invoke_com_retry(llm, prompt, step="relatorio_final")
    exec_summary = response.content.strip() or (
        "Migration review completed. See findings by severity below."
    )

    conteudo = _montar_relatorio_markdown(state, exec_summary)
    _log_progress("relatorio_final", f"fim — relatório {len(conteudo)} chars")

    if state.get("deve_reprocessar"):
        aviso = (
            "# ⚠️ ACTION REQUIRED: Remigration\n\n"
            "> Achados **P0/P1** detectados. O orquestrador deve reiniciar "
            "**migration → test → review** (até 3 ciclos) com o `feedback_migracao` abaixo.\n\n---\n\n"
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

    workflow.add_edge("no_critico",       "relatorio_final")
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
        "feedback_migracao": "",
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
        "feedback_migracao":  result.get("feedback_migracao", ""),
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
