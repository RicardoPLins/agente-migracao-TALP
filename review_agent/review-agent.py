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
import textwrap
import threading
import time
import urllib.error
import urllib.request
import shutil
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Literal, TypedDict

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel


# ─────────────────────────────────────────────────────────────────────────────
# Verificação de Dependencias externas
# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

def _verificar_dependencias_cli() -> None:
    """Verifica se as ferramentas CLI externas obrigatórias estão disponíveis no PATH."""
    faltantes = []
    
    if not shutil.which("git"):
        faltantes.append("git")
    if not shutil.which("ruff"):
        faltantes.append("ruff")
        
    if faltantes:
        msg = (
            f"FALTA DE DEPENDÊNCIAS CRÍTICAS: As seguintes ferramentas não foram "
            f"encontradas no PATH: {', '.join(faltantes)}. O sistema vai degradar a "
            "análise estrutural ou ignorar completamente a verificação de lint."
        )
        raise RuntimeError(msg)
        
_verificar_dependencias_cli()

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


def _load_regras_migracao() -> str:
    """Contrato fixo urllib→requests usado pelos prompts de revisão (não altera migration_agent)."""
    path = _PROMPTS_DIR / "regras_migracao.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _load_rubrica_evidencia() -> str:
    path = _PROMPTS_DIR / "rubrica_evidencia.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _render(key: str, **kwargs: str) -> str:
    """
    Renderiza um template de prompt substituindo os placeholders <<variavel>>
    pelos valores fornecidos.
    """
    kwargs.setdefault("regras_migracao", _REGRAS_MIGRACAO)
    kwargs.setdefault("rubrica_evidencia", _RUBRICA_EVIDENCIA)
    text = "\n".join(_PROMPTS[key])
    for var, val in kwargs.items():
        text = text.replace(f"<<{var}>>", val)
    return text


_load_prompts()
_REGRAS_MIGRACAO = _load_regras_migracao()
_RUBRICA_EVIDENCIA = _load_rubrica_evidencia()


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


def _listar_modelos_ollama(base_url: str = "http://localhost:11434") -> list[str]:
    """Lista modelos instalados no Ollama (API /api/tags)."""
    url = base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []


def _resolver_modelo_ollama(solicitado: str, base_url: str) -> str:
    """
    Resolve nome do modelo Ollama. Aceita match exato, prefixo ou família qwen2.5*7b.
    """
    modelos = _listar_modelos_ollama(base_url)
    if not modelos:
        raise ValueError(
            "Ollama não respondeu em "
            f"{base_url}. Inicie o Ollama e instale um modelo "
            f"(ex.: ollama pull {solicitado})."
        )

    if solicitado in modelos:
        return solicitado

    # tag curta vs nome completo (qwen2.5-coder:7b vs ...:latest)
    for nome in modelos:
        if nome.split(":")[0] == solicitado.split(":")[0] and solicitado.split(":")[-1] in nome:
            return nome
        if nome.startswith(solicitado) or solicitado.startswith(nome.split(":")[0]):
            if ":" not in solicitado or solicitado.split(":")[-1] in nome:
                return nome

    prefs = [solicitado]
    if solicitado == "qwen2.5:7b":
        prefs.extend(["qwen2.5-coder:7b", "qwen2.5:7b-instruct", "qwen2.5:latest"])

    for pref in prefs:
        if pref in modelos:
            return pref
        for nome in modelos:
            if pref.split(":")[0] in nome.lower() and (
                ":" not in pref or pref.split(":")[-1] in nome
            ):
                return nome

    lista = ", ".join(modelos)
    raise ValueError(
        f"Modelo Ollama '{solicitado}' não instalado. "
        f"Disponíveis: {lista}. "
        f"Instale com: ollama pull {solicitado}"
    )


def _get_llm() -> BaseChatModel:
    """
    Backend LLM do review agent.

    REVIEW_LLM_PROVIDER=groq (default) → Groq via API_3
    REVIEW_LLM_PROVIDER=ollama         → Ollama local (ex.: qwen2.5:7b)
    """
    provider = os.getenv("REVIEW_LLM_PROVIDER", "groq").strip().lower()
    temperature = float(os.getenv("REVIEW_LLM_TEMPERATURE", "0"))

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        solicitado = os.getenv("REVIEW_OLLAMA_MODEL", "qwen2.5:7b")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        model = _resolver_modelo_ollama(solicitado, base_url)
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "base_url": base_url,
        }
        num_ctx = os.getenv("REVIEW_OLLAMA_NUM_CTX")
        if num_ctx:
            kwargs["num_ctx"] = int(num_ctx)
        return ChatOllama(**kwargs)

    if provider == "groq":
        api_key = os.getenv("API_3")
        if not api_key:
            raise ValueError(
                "API_3 não encontrada nas variáveis de ambiente. "
                "Use REVIEW_LLM_PROVIDER=ollama para Ollama local."
            )
        model = os.getenv("REVIEW_GROQ_MODEL", "llama-3.3-70b-versatile")
        return ChatGroq(
            api_key=api_key,
            model_name=model,
            temperature=temperature,
        )

    raise ValueError(
        f"REVIEW_LLM_PROVIDER inválido: {provider!r}. Use 'groq' ou 'ollama'."
    )


_LLM_CLOUD_LOCK = threading.Lock()
_ULTIMA_CHAMADA_LLM_CLOUD = 0.0


def _llm_provider_cloud() -> bool:
    """Hoje, provider cloud suportado pelo review agent é Groq."""
    return os.getenv("REVIEW_LLM_PROVIDER", "groq").strip().lower() == "groq"


def _intervalo_chamada_cloud() -> float:
    raw = os.getenv("REVIEW_CLOUD_REQUEST_INTERVAL_SECONDS", "45")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 45.0


def _invocar_llm(llm: BaseChatModel, prompt: str) -> Any:
    """
    Invoca o LLM respeitando rate limit em providers cloud.

    No Groq free/on_demand, chamadas consecutivas dos nós do grafo podem estourar
    TPM. O lock também serializa fan-out paralelo do LangGraph.
    """
    if not _llm_provider_cloud():
        return llm.invoke(prompt)

    global _ULTIMA_CHAMADA_LLM_CLOUD
    intervalo = _intervalo_chamada_cloud()

    with _LLM_CLOUD_LOCK:
        agora = time.monotonic()
        espera = intervalo - (agora - _ULTIMA_CHAMADA_LLM_CLOUD)
        if _ULTIMA_CHAMADA_LLM_CLOUD > 0 and espera > 0:
            logger.info(
                "Aguardando %.1fs antes da próxima chamada ao LLM cloud.",
                espera,
            )
            time.sleep(espera)

        try:
            return llm.invoke(prompt)
        finally:
            _ULTIMA_CHAMADA_LLM_CLOUD = time.monotonic()


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
    status_qualidade: str   # "" | "aprovado" | "requer_refinamento"
    iteracao: int           # Incrementado pelo Roteador; máximo efetivo = 3
    deve_reprocessar: bool  # True = no_critico sinaliza que o migration_agent deve refazer

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
        
        logger.error(f"Falha inesperada no git diff. Exit code: {result.returncode}, Stderr: {result.stderr.strip()}")
        return None

    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("Timeout de 15s excedido ao executar git diff.")
        return None
    except FileNotFoundError:
        logger.error("Binário 'git' não encontrado no PATH durante a execução do _run_git_diff.")
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
    1. `git diff --no-index` gera o diff exato e determinístico entre os dois
       arquivos (sem alucinações de LLM na identificação das mudanças).
    2. O LLM recebe o diff como fonte primária e extrai o JSON estruturado,
       usando os códigos completos apenas como contexto semântico adicional.
    3. Fallback: se git não estiver disponível, o LLM compara os códigos
       diretamente (comportamento anterior).
    """
    raw_diff = _run_git_diff(state["codigo_original"], state["codigo_migrado"])

    llm = _get_llm()
    prompt = _render(
        "parser",
        codigo_original=state["codigo_original"],
        codigo_migrado=state["codigo_migrado"],
        raw_diff=raw_diff or "(git não disponível — analise os códigos diretamente)",
    )
    try:
        response = _invocar_llm(llm, prompt)
        diff = json.loads(_strip_md_fences(response.content))
    except json.JSONDecodeError:
        logger.error(f"Falha na invocação do LLM ou no parsing do no_parser: {str(e)}")
        diff = {"raw": response.content, "parse_error": True}

    return {
        "raw_diff":        raw_diff or "",
        "diff_estruturado": diff,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Nó 2 – Classificador (Semântico Leve)
# ─────────────────────────────────────────────────────────────────────────────

_ALIASES_AGENTE_CLASSIFICADOR: dict[str, str] = {
    "semantics": "semantica",
    "semantic": "semantica",
    "semantica": "semantica",
    "security": "seguranca",
    "seguranca": "seguranca",
    "lint": "lint",
    "style": "lint",
}


def _normalizar_agentes_classificador(parsed: dict) -> list[str]:
    """
    Converte resposta do classificador (EN/PT) para chaves internas do grafo.
    O prompt usa \"agents\": [\"semantics\", \"security\", \"lint\"].
    """
    bruto = parsed.get("agentes") or parsed.get("agents") or []
    if isinstance(bruto, str):
        bruto = [bruto]
    agentes: list[str] = []
    for item in bruto:
        if not isinstance(item, str):
            continue
        canon = _ALIASES_AGENTE_CLASSIFICADOR.get(item.strip().lower())
        if canon and canon not in agentes:
            agentes.append(canon)
    return agentes


def _agentes_por_diff_deterministico(diff: dict) -> list[str]:
    """
    Regras do classificador.json em Python — garante cobertura mínima
    quando o LLM omite agentes (comum em modelos 7B locais).
    """
    if not isinstance(diff, dict):
        return []
    if diff.get("parse_error"):
        return ["semantica", "seguranca", "lint"]

    agentes: list[str] = []
    altered = diff.get("altered_functions") or []
    added = diff.get("added_functions") or []
    removed = diff.get("removed_functions") or []
    altered_cls = diff.get("altered_classes") or []
    added_deps = diff.get("added_dependencies") or []
    removed_deps = diff.get("removed_dependencies") or []
    impact = (diff.get("impact_summary") or "").lower()

    if altered or added or removed or altered_cls:
        agentes.extend(["semantica", "lint"])

    if added_deps or removed_deps:
        agentes.append("seguranca")

    simbolos = [s.lower() for s in (*altered, *added) if isinstance(s, str)]
    termos_sec = (
        "request", "fetch", "execute", "post", "get", "upload", "download",
        "log", "logger", "write", "read", "scrape", "auth", "cookie",
    )
    if any(any(t in sym for t in termos_sec) for sym in simbolos):
        agentes.append("seguranca")

    termos_impacto = (
        "http", "request", "urllib", "network", "authentication", "cookie",
        "sensitive", "credential",
    )
    if any(t in impact for t in termos_impacto):
        agentes.append("seguranca")

    out: list[str] = []
    for a in agentes:
        if a not in out:
            out.append(a)
    return out


def no_classificador(state: CodeReviewState) -> dict:
    """
    Lê o diff estruturado e decide quais agentes especialistas devem ser
    acionados, preenchendo `agentes_acionados`.

    Fallback seguro: se o Parser retornou parse_error (diff inválido),
    aciona todos os agentes sem consultar o LLM — melhor gastar tokens
    a mais do que perder cobertura de análise.
    """
    if state["diff_estruturado"].get("parse_error"):
        return {"agentes_acionados": ["semantica", "seguranca", "lint"]}

    if os.getenv("REVIEW_FORCE_ALL_AGENTS", "").strip().lower() in ("1", "true", "yes"):
        return {"agentes_acionados": ["semantica", "seguranca", "lint"]}

    llm = _get_llm()
    diff_str = json.dumps(state["diff_estruturado"], ensure_ascii=False, indent=2)
    prompt = _render("classificador", diff_str=diff_str)
    response = _invocar_llm(llm, prompt)
    try:
        parsed = json.loads(_strip_md_fences(response.content))
        agentes = _normalizar_agentes_classificador(parsed)
        if not agentes:
            agentes = ["semantica", "seguranca", "lint"]
    except (json.JSONDecodeError, AttributeError):
        agentes = ["semantica", "seguranca", "lint"]

    for a in _agentes_por_diff_deterministico(state["diff_estruturado"]):
        if a not in agentes:
            agentes.append(a)

    return {"agentes_acionados": agentes}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 3 – Roteador (preparação de iteração + fan-out via Send)
# ─────────────────────────────────────────────────────────────────────────────

def no_roteador(state: CodeReviewState) -> dict:
    """
    Prepara o estado para uma nova rodada de análise:
    - Incrementa o contador de iteração.
    - Limpa os achados anteriores para evitar acúmulo entre rodadas.
    """
    return {
        "iteracao": state.get("iteracao", 0) + 1,
        "achados_semantica": [],
        "achados_seguranca": [],
        "achados_lint": [],
    }


def _despachar_agentes(state: CodeReviewState) -> list[Send]:
    """
    Função de roteamento condicional: gera um `Send` para cada agente listado
    em `agentes_acionados`, possibilitando execução paralela.
    Caso a lista esteja vazia, encaminha diretamente ao Nó Crítico.
    """
    mapa_agentes = {
        "semantica": "no_semantico",
        "seguranca": "no_seguranca",
        "lint":      "no_lint",
    }
    envios = [
        Send(mapa_agentes[agente], state)
        for agente in state.get("agentes_acionados", [])
        if agente in mapa_agentes
    ]
    return envios if envios else [Send("no_critico", state)]


# ─────────────────────────────────────────────────────────────────────────────
# Trechos de código para especialistas (economia de tokens)
# ─────────────────────────────────────────────────────────────────────────────

_DEF_RE = re.compile(r"^(\s*)def\s+(\w+)\s*\(")


def _extrair_bloco_def(linhas: list[str], nome: str) -> tuple[int, list[str]] | None:
    """Retorna (linha_inicial_1based, linhas_do_bloco) de uma definição def nome()."""
    for i, linha in enumerate(linhas):
        m = _DEF_RE.match(linha)
        if m and m.group(2) == nome:
            indent = m.group(1)
            bloco = [linha]
            for j in range(i + 1, len(linhas)):
                prox = linhas[j]
                if not prox.strip():
                    bloco.append(prox)
                    continue
                # Fim do bloco: qualquer código no mesmo nível (ou externo) ao `def`
                if len(prox) - len(prox.lstrip()) <= len(indent):
                    if not prox.lstrip().startswith("#"):
                        break
                bloco.append(prox)
            return i + 1, bloco
    return None


def _extrair_trechos_funcoes(codigo_migrado: str, diff_estruturado: dict) -> str:
    """
    Extrai trechos numerados apenas das funções alteradas/adicionadas.
    Evita enviar o arquivo migrado inteiro aos agentes semântica/segurança.
    """
    simbolos: list[str] = []
    if isinstance(diff_estruturado, dict):
        for chave in ("altered_functions", "added_functions"):
            for nome in diff_estruturado.get(chave, []) or []:
                if isinstance(nome, str) and nome not in simbolos:
                    simbolos.append(nome)

    if not simbolos:
        return (
            "(Nenhuma função listada no diff estruturado — "
            "use o GIT DIFF acima como fonte primária.)"
        )

    linhas = codigo_migrado.splitlines()
    trechos: list[str] = []
    for nome in simbolos:
        bloco_info = _extrair_bloco_def(linhas, nome)
        if not bloco_info:
            trechos.append(f"# --- `{nome}` — não encontrada no código migrado ---")
            continue
        inicio, bloco = bloco_info
        fim = inicio + len(bloco) - 1
        numeradas = [f"{inicio + k:4d}| {l}" for k, l in enumerate(bloco)]
        trechos.append(
            f"# --- `{nome}` (linhas {inicio}-{fim} do arquivo migrado) ---\n"
            + "\n".join(numeradas)
        )
    return "\n\n".join(trechos)


def _bloco_funcao(codigo: str, nome: str) -> str:
    info = _extrair_bloco_def(codigo.splitlines(), nome)
    if not info:
        return ""
    _, bloco = info
    return "\n".join(bloco)


def _nomes_funcoes(codigo: str) -> list[str]:
    nomes: list[str] = []
    for linha in codigo.splitlines():
        m = _DEF_RE.match(linha)
        if m and m.group(2) not in nomes:
            nomes.append(m.group(2))
    return nomes


class _FluxoLoopFuncao(ast.NodeVisitor):
    """Conta continue/return dentro de while loops em uma def."""

    def __init__(self) -> None:
        self.continues_in_while = 0
        self.returns_in_while = 0
        self._depth_while = 0

    def visit_While(self, node: ast.While) -> None:
        self._depth_while += 1
        self.generic_visit(node)
        self._depth_while -= 1

    def visit_Continue(self, node: ast.Continue) -> None:
        if self._depth_while:
            self.continues_in_while += 1

    def visit_Return(self, node: ast.Return) -> None:
        if self._depth_while:
            self.returns_in_while += 1


def _stats_loop_funcao(bloco: str) -> _FluxoLoopFuncao | None:
    try:
        tree = ast.parse(textwrap.dedent(bloco))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            visitor = _FluxoLoopFuncao()
            visitor.visit(node)
            return visitor
    return None


def _checar_http_sem_raise(
    codigo_original: str, codigo_migrado: str, nome: str
) -> dict[str, Any] | None:
    """urlopen (HTTPError em 4xx/5xx) → requests sem raise_for_status."""
    bloco_orig = _bloco_funcao(codigo_original, nome)
    bloco_mig = _bloco_funcao(codigo_migrado, nome)
    if not bloco_orig or not bloco_mig:
        return None
    if "urlopen" not in bloco_orig:
        return None
    if not re.search(r"requests\.(get|post|put|patch|delete|request)\s*\(", bloco_mig):
        return None
    if "raise_for_status" in bloco_mig:
        return None
    if re.search(r"\.status_code\b|\.ok\b|\bHTTPError\b|status_code\s*[!=<>]", bloco_mig):
        return None

    indice = _indice_simbolos_migrado(codigo_migrado)
    return {
        "prefixo": "COMPAT",
        "severidade": "P1",
        "simbolo": nome,
        "linha_llm": None,
        "linha": indice.get(nome),
        "linha_corrigida": False,
        "descricao": (
            "urllib.request.urlopen levanta HTTPError em 4xx/5xx; "
            "requests.post/get sem raise_for_status() ignora status HTTP de erro"
        ),
        "trigger": "urlopen substituído por requests sem verificação de status (4xx/5xx)",
        "agente": "deterministico",
        "raw": "",
    }


def _checar_early_return_loop(
    codigo_original: str, codigo_migrado: str, nome: str
) -> dict[str, Any] | None:
    """Erro no while usava continue (retry); migrado usa return (aborta loop/função)."""
    bloco_orig = _bloco_funcao(codigo_original, nome)
    bloco_mig = _bloco_funcao(codigo_migrado, nome)
    if not bloco_orig or not bloco_mig:
        return None
    if "while" not in bloco_orig or "while" not in bloco_mig:
        return None

    stats_orig = _stats_loop_funcao(bloco_orig)
    stats_mig = _stats_loop_funcao(bloco_mig)
    if not stats_orig or not stats_mig:
        return None
    if stats_orig.continues_in_while == 0:
        return None
    if stats_mig.returns_in_while == 0:
        return None
    if stats_orig.returns_in_while > 0:
        return None

    indice = _indice_simbolos_migrado(codigo_migrado)
    return {
        "prefixo": "COMPAT",
        "severidade": "P1",
        "simbolo": nome,
        "linha_llm": None,
        "linha": indice.get(nome),
        "linha_corrigida": False,
        "descricao": (
            "Tratamento de erro no loop de payload/actions usa return em vez de continue — "
            "interrompe retry do while que existia no original"
        ),
        "trigger": "return antecipado no loop; original usava continue para retry após erro",
        "agente": "deterministico",
        "raw": "",
    }


def _detectar_regressoes_deterministicas(
    codigo_original: str, codigo_migrado: str,
) -> list[dict[str, Any]]:
    """
    Regras diff-based que não dependem do LLM — cobrem regressões conhecidas
    (ex.: test1 http-no-raise, early-return-loop).
    """
    nomes_orig = set(_nomes_funcoes(codigo_original))
    achados: list[dict[str, Any]] = []
    vistos: set[tuple[str, str]] = set()

    for nome in _nomes_funcoes(codigo_migrado):
        if nome not in nomes_orig:
            continue
        for checker in (_checar_http_sem_raise, _checar_early_return_loop):
            candidato = checker(codigo_original, codigo_migrado, nome)
            if not candidato:
                continue
            chave = (candidato["prefixo"], candidato.get("simbolo") or "")
            if chave in vistos:
                continue
            vistos.add(chave)
            candidato["formatado"] = _formatar_achado(candidato)
            achados.append(candidato)
    return achados


def _mesclar_achados_deterministicos(
    codigo_original: str,
    codigo_migrado: str,
    todos: list[dict[str, Any]],
    sem_fmt: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Anexa regressões determinísticas e substitui versões LLM mais fracas."""
    extras = _detectar_regressoes_deterministicas(codigo_original, codigo_migrado)
    for achado in extras:
        simbolo = achado.get("simbolo") or ""
        desc_extra = (achado.get("descricao") or "").lower()

        def mesmo_problema(a: dict[str, Any]) -> bool:
            if (a.get("simbolo") or "") != simbolo:
                return False
            texto = f"{a.get('descricao') or ''}. {a.get('trigger') or ''}".lower()
            if "raise_for_status" in desc_extra:
                return bool(re.search(r"raise_for_status|4xx|5xx|status|requests", texto))
            if "return em vez de continue" in desc_extra:
                return bool(re.search(r"\breturn\b|\bcontinue\b|time\.sleep|retry|loop", texto))
            return a.get("prefixo") == achado.get("prefixo")

        removidos = [a for a in todos if mesmo_problema(a)]
        if removidos:
            todos[:] = [a for a in todos if not mesmo_problema(a)]
            if sem_fmt is not None:
                removidos_fmt = {a.get("formatado") for a in removidos}
                sem_fmt[:] = [item for item in sem_fmt if item not in removidos_fmt]

        todos.append(achado)
        if sem_fmt is not None:
            sem_fmt.append(achado["formatado"])
    return todos


def _downgrade_severidade(sev: str, steps: int = 1) -> str:
    order = ["P0", "P1", "P2", "P3"]
    idx = order.index(sev) if sev in order else 3
    return order[min(idx + steps, 3)]


def _tem_evidencia_forte(texto: str) -> bool:
    return bool(_EVIDENCIA_FORTE_RE.search(texto))


def _checar_contrato_http(bloco: str) -> dict[str, str] | None:
    """Regras determinísticas de Content-Type / payload (eleva severidade com evidência)."""
    bloco_lower = bloco.lower()
    usa_json_dumps = "json.dumps" in bloco
    ct_form = (
        "application/x-www-form-urlencoded" in bloco_lower
        or "urlencode" in bloco_lower
    )
    ct_json = "application/json" in bloco_lower
    # data= com identificador no bloco; usa_json_dumps já avalia o bloco inteiro
    data_form = bool(re.search(r"data\s*=\s*[a-zA-Z_]\w*", bloco))

    if ct_form and usa_json_dumps and not ct_json:
        return {
            "prefixo": "CONTRACT",
            "severidade": "P0",
            "descricao": (
                "JSON body (json.dumps) enviado com semântica form-urlencoded/urlencode — "
                "servidor interpreta corpo incorretamente"
            ),
            "trigger": "POST/PUT com payload JSON e header/body no formato form",
        }
    if ct_json and data_form and not usa_json_dumps:
        return {
            "prefixo": "CONTRACT",
            "severidade": "P1",
            "descricao": (
                "Content-Type application/json mas corpo enviado como form dict/urlencode "
                "sem json.dumps"
            ),
            "trigger": "request com Content-Type JSON e data= dict/urlencoded",
        }
    return None


def _achado_toca_fluxo_de_loop(texto: str) -> bool:
    return bool(
        re.search(
            r"\b(return|continue|retry|loop|while|time\.sleep|error handling|payload|actions)\b",
            texto,
            re.I,
        )
    )


def _achado_content_type_form_normal(texto: str, bloco: str) -> bool:
    """
    `requests.post(..., data=dict)` com Content-Type form-urlencoded é uso normal.
    Não deve virar P1 só por usar `data=`; o problema real seria status/JSON errado.
    """
    texto_lower = texto.lower()
    bloco_lower = bloco.lower()
    fala_de_form_data = (
        "content-type" in texto_lower
        and "data" in texto_lower
        and (
            "form-urlencoded" in texto_lower
            or "inconsistent request behavior" in texto_lower
        )
    )
    if not fala_de_form_data:
        return False
    if "application/x-www-form-urlencoded" not in bloco_lower:
        return False
    if not re.search(
        r"requests\.(get|post|put|patch|delete|request)\s*\([^)]*\bdata\s*=",
        bloco,
        re.S,
    ):
        return False
    if "json.dumps" in bloco or "application/json" in bloco_lower:
        return False
    return not re.search(r"raise_for_status|4xx|5xx|status_code|\.ok\b", texto_lower)


def _validar_e_reclassificar_achado(
    achado: dict[str, Any],
    codigo_migrado: str,
    codigo_original: str | None = None,
) -> dict[str, Any] | None:
    """
    Filtra achados genéricos/fracos e aplica regras contextuais determinísticas.
    Retorna None para descartar o achado.
    """
    prefixo = achado.get("prefixo") or ""
    sev = achado.get("severidade") or "P3"
    desc = achado.get("descricao") or ""
    trigger = achado.get("trigger") or ""
    texto = f"{desc}. {trigger}"

    if prefixo == "INFO" and "no relevant" in texto.lower():
        return None

    if not achado.get("prefixo") and not _tem_evidencia_forte(texto):
        return None

    # Elevação contextual por corpo da função
    simbolo = achado.get("simbolo")
    if simbolo:
        bloco = _bloco_funcao(codigo_migrado, simbolo)
        if bloco:
            if codigo_original and _achado_toca_fluxo_de_loop(texto):
                fluxo = _checar_early_return_loop(codigo_original, codigo_migrado, simbolo)
                if fluxo:
                    fluxo["raw"] = achado.get("raw", "")
                    fluxo["agente"] = achado.get("agente", "deterministico")
                    return fluxo

            if _achado_content_type_form_normal(texto, bloco):
                return None

            contrato = _checar_contrato_http(bloco)
            if contrato:
                achado["prefixo"] = contrato["prefixo"]
                achado["severidade"] = contrato["severidade"]
                achado["descricao"] = contrato["descricao"]
                achado["trigger"] = contrato["trigger"]
                sev = achado["severidade"]
                desc = achado["descricao"]
                trigger = achado["trigger"]
                texto = f"{desc}. {trigger}"

    # Observabilidade → no máximo P3
    if _OBSERVABILIDADE_RE.search(texto) and sev in ("P0", "P1"):
        achado["severidade"] = "P3"
        achado["prefixo"] = "INFO"
        return achado

    # Troca genérica urllib→requests sem evidência forte
    if _GENERICO_SWAP_RE.search(texto) and not _tem_evidencia_forte(texto):
        return None

    # Linguagem fraca em P0/P1 sem evidência concreta → descartar
    if sev in ("P0", "P1") and _LINGUAGEM_FRACA_RE.search(texto):
        if not _tem_evidencia_forte(texto):
            return None
        achado["severidade"] = _downgrade_severidade(sev, 1)
        return achado

    # P0 exige evidência forte
    if sev == "P0" and not _tem_evidencia_forte(texto):
        achado["severidade"] = "P1"

    # Trigger vago em P1 → rebaixa
    if sev == "P1" and len(trigger.split()) < 4 and not _tem_evidencia_forte(texto):
        achado["severidade"] = "P2"

    # "error handling changed" sem exceção nomeada → P2 no máximo
    if re.search(r"error handling .+ changed", texto, re.I) and not re.search(
        r"\b(KeyError|TypeError|HTTPError|URLError|JSONDecodeError)\b", texto, re.I
    ):
        if achado["severidade"] in ("P0", "P1"):
            achado["severidade"] = "P2"

    return achado


def _pipeline_achados(
    achados: list[str],
    agente: str,
    codigo_migrado: str,
    codigo_original: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Parse único + validação contextual + correção de linhas (com fallback para bad parsing)."""
    indice = _indice_simbolos_migrado(codigo_migrado)
    formatados: list[str] = []
    estruturados: list[dict[str, Any]] = []
    
    for raw in achados:
        raw = raw.strip()
        # Removemos a restrição estrita de `raw.startswith("- [")`
        if not raw:
            continue
            
        parsed = _corrigir_linha_achado(_parse_achado(raw, agente), indice)
        
        # Fallback de Resgate: Se a regex não encontrou os colchetes, 
        # salvamos a linha bruta como um achado válido de baixa severidade.
        if not parsed.get("prefixo"):
            # Limpa qualquer variação de bullet point (*, +, -, >) no começo da string
            descricao_limpa = raw.lstrip("-*+> ").strip()
            
            # Evita capturar linhas vazias ou apenas hífens soltos
            if not descricao_limpa or len(descricao_limpa) < 5:
                continue
                
            parsed["prefixo"] = "INFO"
            parsed["severidade"] = "P3"
            parsed["descricao"] = descricao_limpa
            # O campo formatado será reescrito corretamente abaixo
        
        validado = _validar_e_reclassificar_achado(
            parsed, codigo_migrado, codigo_original
        )
        if validado is None:
            continue
            
        validado["formatado"] = _formatar_achado(validado)
        estruturados.append(validado)
        formatados.append(validado["formatado"])
        
    return formatados, estruturados

# ─────────────────────────────────────────────────────────────────────────────
# Nó 4a – no_semantico
# ─────────────────────────────────────────────────────────────────────────────

def no_semantico(state: CodeReviewState) -> dict:
    """
    Analisa a equivalência semântica entre o código original e o migrado.
    Em iterações de refinamento, lê `motivo_rejeicao` para não repetir erros.

    Passa o `raw_diff` (git diff --no-index) para que o agente possa referenciar
    números de linha exatos ao reportar achados — técnica inspirada no PR-Agent
    (pr_reviewer_prompts.toml) que exige start_line/end_line em cada finding.
    """
    llm = _get_llm()
    critica = (
        f"\n⚠️  CRÍTICA DA ITERAÇÃO ANTERIOR — corrija os pontos abaixo:\n{state['motivo_rejeicao']}"
        if state.get("motivo_rejeicao")
        else ""
    )
    diff_str = json.dumps(state["diff_estruturado"], ensure_ascii=False, indent=2)
    trechos = _extrair_trechos_funcoes(state["codigo_migrado"], state["diff_estruturado"])
    prompt = _render(
        "agente_semantica",
        critica=critica,
        diff_str=diff_str,
        raw_diff=state.get("raw_diff") or "(git diff não disponível)",
        trechos_migrados=trechos,
    )
    try:
        response = _invocar_llm(llm, prompt)
        achados = [
            linha.strip()
            for linha in response.content.split("\n")
            if linha.strip().startswith("-")
        ]
    except Exception as e:
        logger.error(f"Erro ao invocar LLM no no_semantico: {str(e)}")
        achados = [f"- [CONTRACT][P0] ⚠️ Falha crítica de infraestrutura no no_semantico: {str(e)}"]
    
    return {"achados_semantica": achados or ["- Sem achados semânticos relevantes."]}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 4b – no_seguranca
# ─────────────────────────────────────────────────────────────────────────────

def no_seguranca(state: CodeReviewState) -> dict:
    """
    Audita riscos de segurança introduzidos pela migração.
    Em iterações de refinamento, lê `motivo_rejeicao` para refinar o foco.

    Passa o `raw_diff` (git diff --no-index) para que o agente referencie
    números de linha exatos — técnica do PR-Agent (pr_reviewer_prompts.toml).
    """
    llm = _get_llm()
    critica = (
        f"\n⚠️  CRÍTICA DA ITERAÇÃO ANTERIOR — corrija os pontos abaixo:\n{state['motivo_rejeicao']}"
        if state.get("motivo_rejeicao")
        else ""
    )
    diff_str = json.dumps(state["diff_estruturado"], ensure_ascii=False, indent=2)
    trechos = _extrair_trechos_funcoes(state["codigo_migrado"], state["diff_estruturado"])
    prompt = _render(
        "agente_seguranca",
        critica=critica,
        diff_str=diff_str,
        raw_diff=state.get("raw_diff") or "(git diff não disponível)",
        trechos_migrados=trechos,
    )
    try:
        response = _invocar_llm(llm, prompt)
        achados = [
            linha.strip()
            for linha in response.content.split("\n")
            if linha.strip().startswith("-")
        ]
    except Exception as e:
        logger.error(f"Falha na invocação do LLM no no_seguranca: {str(e)}")
        achados = [f"- [SECURITY][P0] ⚠️ Falha crítica de infraestrutura no agente de segurança: {str(e)}"]

    return {"achados_seguranca": achados or ["- Sem achados de segurança relevantes."]}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 4c – no_lint  (tool use determinístico via Ruff + interpretação LLM)
# ─────────────────────────────────────────────────────────────────────────────

def _inferir_config_ruff(codigo_original: str, llm: BaseChatModel) -> dict:
    """
    Usa o LLM para inferir o estilo implícito do código original e retorna
    uma configuração Ruff ajustada (line_length, regras, indent_width).
    """
    prompt = _render("agente_lint_config", codigo_original=codigo_original)
    defaults = {"line_length": 88, "select": ["E", "W", "F", "I"], "ignore": [], "indent_width": 4}
    try:
        response = _invocar_llm(llm, prompt)
        config = json.loads(_strip_md_fences(response.content))
        return {
            "line_length":  int(config.get("line_length", defaults["line_length"])),
            "select":       config.get("select", defaults["select"]),
            "ignore":       config.get("ignore", defaults["ignore"]),
            "indent_width": int(config.get("indent_width", defaults["indent_width"])),
        }
    except Exception as e:
        logger.warning(f"Falha ao inferir config do Ruff via LLM. Usando defaults. Erro: {str(e)}")
        return defaults


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
            #f"--indent-width={config.get('indent_width', 4)}",
            f"--select={select}",
            "--no-cache",
        ]
        ignore = config.get("ignore", [])
        if ignore:
            cmd.append(f"--ignore={','.join(ignore)}")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode not in (0, 1):
            logger.error(f"Falha na execução do Ruff (exit code {result.returncode}). Stderr: {result.stderr.strip()}")
            return None
        if result.stdout.strip():
            issues = json.loads(result.stdout)
            # Remove o path absoluto do arquivo temporário de cada issue
            for issue in issues:
                issue.pop("filename", None)
            return issues
        return []

    except subprocess.TimeoutExpired:
        logger.error("Timeout de 30s excedido ao executar verificação do Ruff.")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Ruff retornou saída não-JSON inválida. Erro: {str(e)} | Stdout: {result.stdout.strip()[:200]}")
        return None
    except FileNotFoundError:
        logger.error("Binário 'ruff' não encontrado no PATH durante a execução.")
        return None
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
    """
    llm = _get_llm()

    # 1 – Inferir estilo e gerar config Ruff
    ruff_config = _inferir_config_ruff(state["codigo_original"], llm)

    # 2 – Executar Ruff em ambos os arquivos
    issues_original = _run_ruff(state["codigo_original"], ruff_config)
    issues_migrado  = _run_ruff(state["codigo_migrado"],  ruff_config)

    # 3 – Filtrar apenas novos issues
    novos_issues = _filtrar_novos_issues(issues_original, issues_migrado)

    if not novos_issues:
        return {"achados_lint": ["- Nenhum novo issue de lint/style introduzido pela migração."]}

    # 4 – LLM interpreta os novos issues (severidade + contexto)
    critica = (
        f"\n⚠️  CRÍTICA DA ITERAÇÃO ANTERIOR — corrija os pontos abaixo:\n{state['motivo_rejeicao']}"
        if state.get("motivo_rejeicao")
        else ""
    )
    prompt = _render(
        "agente_lint_interpretacao",
        critica=critica,
        novos_issues=json.dumps(novos_issues, ensure_ascii=False, indent=2),
        estilo_inferido=json.dumps(ruff_config, ensure_ascii=False, indent=2),
        codigo_migrado=state["codigo_migrado"],
    )
    try:
        response = _invocar_llm(llm, prompt)
        achados = [
            linha.strip()
            for linha in response.content.split("\n")
            if linha.strip().startswith("-")
        ]
    except Exception as e:
        logger.error(f"Falha na invocação do LLM no no_lint: {str(e)}")
        # Fallback: O LLM caiu, mas temos os dados brutos do Ruff.
        achados = [
            f"- [INFO][P3] ⚠️ API indisponível para interpretar lint: {str(e)}",
            f"- [INFO][P3] Ruff detectou {len(novos_issues)} novo(s) problema(s) de formatação não interpretado(s)."
        ]
    return {"achados_lint": achados or ["- Nenhum novo issue relevante de lint/style identificado."]}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 5 – Nó Crítico (Reflection / QA da Revisão)
# ─────────────────────────────────────────────────────────────────────────────

_MAX_ITERACOES = 3


def no_critico(state: CodeReviewState) -> dict:
    """
    Ponto de convergência e Reflection da revisão.

    - Aprova se os achados forem precisos, específicos e acionáveis.
    - Rejeita com `motivo_rejeicao` se houver falsos positivos, achados vagos
      ou redundâncias, para que os agentes refinem na próxima iteração.
    - Na iteração máxima, avalia normalmente com o LLM: se ainda rejeitar,
      sinaliza `deve_reprocessar = True` para que o migration_agent refaça a
      migração — ao invés de forçar uma aprovação sem embasamento.
    """
    llm = _get_llm()

    achados_str = "\n".join([
        "### Semântica:",
        *state.get("achados_semantica", ["(não acionado)"]),
        "",
        "### Segurança:",
        *state.get("achados_seguranca", ["(não acionado)"]),
        "",
        "### Lint/Style:",
        *state.get("achados_lint", ["(não acionado)"]),
    ])

    prompt = _render(
        "no_critico",
        iteracao=str(state.get("iteracao", 0)),
        achados_str=achados_str,
    )
    response = _invocar_llm(llm, prompt)
    try:
        resultado = json.loads(_strip_md_fences(response.content))
        decisao = resultado.get("decisao", "aprovado")
        motivo  = resultado.get("motivo_rejeicao", "")
    except json.JSONDecodeError:
        # Fail-secure: se não conseguimos ler o veredito, forçamos a rejeição.
        decisao = "requer_refinamento"
        motivo  = (
            "Erro de infraestrutura: O agente crítico falhou ao gerar um JSON válido "
            "na avaliação dos achados. O LLM deve tentar novamente."
        )

    # Iteração máxima atingida: não podemos mais fazer loop.
    # Se o LLM ainda rejeita, sinaliza deve_reprocessar ao migration_agent.
    if state.get("iteracao", 0) >= _MAX_ITERACOES and decisao == "requer_refinamento":
        return {
            "status_qualidade":  "aprovado",   # força saída do loop
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
        state.get("status_qualidade") == "aprovado"
        or state.get("iteracao", 0) >= _MAX_ITERACOES
    ):
        return "relatorio_final"
    return "no_roteador"


# ─────────────────────────────────────────────────────────────────────────────
# Relatório final — template determinístico + correção de linhas
# ─────────────────────────────────────────────────────────────────────────────

ACHADO_RE = re.compile(
    r"^-\s*\[(?P<prefix>[^\]]+)\]\[(?P<sev>P[0-3])\]\s*"
    r"(?:`(?P<symbol>[^`]+)`\s*)?"
    r"(?:\((?:line|linha)\s+(?P<line_paren>\d+)(?:\s+of\s+migrated)?\)"
    r"|(?:\b(?:line|linha)\s+(?P<line_plain>\d+)))?"
    r"(?:\s*_\([^)]*modelo citou linha \d+\)_)?"
    r"\s*[—–-]\s*(?P<body>.+)$",
    re.IGNORECASE,
)

_LINGUAGEM_FRACA_RE = re.compile(
    r"\b(may not work|might not|could lead to|potentially|possibly|"
    r"may impact|can cause unexpected|potentially leading|"
    r"differences in behavior|without proper handling|as expected|"
    r"may cause|could cause)\b",
    re.I,
)

_EVIDENCIA_FORTE_RE = re.compile(
    r"\b(KeyError|TypeError|AttributeError|ValueError|JSONDecodeError|"
    r"HTTPError|ConnectionError|ChunkedEncodingError|UnicodeDecodeError)\b|"
    r"Content-Type|application/x-www-form-urlencoded|application/json|"
    r"urlencode|json\.dumps|returns .+ instead of|status_code|\.json\(\)|"
    r"\.text\b|\.content\b|gzip|GzipFile|double decompression",
    re.I,
)

_GENERICO_SWAP_RE = re.compile(
    r"\b(changed from urllib|urllib\.request to requests|"
    r"requests instead of urllib|library (?:was )?changed|"
    r"logging .+ changed|initialization .+ changed)\b",
    re.I,
)

_OBSERVABILIDADE_RE = re.compile(
    r"\b(logging|log message|comment|naming|cosmetic|observability)\b",
    re.I,
)

_LEGENDA_SEVERIDADE = """\
| Nível | Etiqueta | Significado | Ação recomendada |
|-------|----------|-------------|------------------|
| **P0** | Quebra garantida | Crash, formato HTTP errado, perda/corrupção de dados | Corrigir **antes** do merge |
| **P1** | Quebra provável | Mudança silenciosa de retorno/contrato com evidência no diff | Corrigir **antes** do merge |
| **P2** | Robustez | Tratamento de erro, timeout, retry degradado (novo no diff) | Corrigir se possível |
| **P3** | Style / observabilidade | Logging, naming, sugestões sem impacto funcional | Opcional |
"""


def _indice_simbolos_migrado(codigo_migrado: str) -> dict[str, int]:
    """Mapeia nomes de funções/métodos para a linha de definição no código migrado."""
    indice: dict[str, int] = {}
    for num, linha in enumerate(codigo_migrado.splitlines(), start=1):
        m = re.match(r"^\s*def\s+(\w+)\s*\(", linha)
        if m:
            indice[m.group(1)] = num
    return indice


def _parse_achado(raw: str, agente: str) -> dict[str, Any]:
    """Extrai campos estruturados de uma linha de achado."""
    raw = raw.strip()
    base: dict[str, Any] = {
        "raw": raw,
        "agente": agente,
        "prefixo": "",
        "severidade": "P3",
        "simbolo": None,
        "linha_llm": None,
        "linha": None,
        "linha_corrigida": False,
        "descricao": raw.lstrip("- ").strip(),
        "trigger": "",
        "formatado": raw,
    }
    if not raw.startswith("- "):
        return base

    m = ACHADO_RE.match(raw)
    if not m:
        return base

    body = m.group("body").strip()
    trigger = ""
    if re.search(r"\bTrigger\s*:", body, re.IGNORECASE):
        partes = re.split(r"\s+Trigger\s*:", body, maxsplit=1, flags=re.IGNORECASE)
        if len(partes) == 2:
            body, trigger = partes[0].strip().rstrip("."), partes[1].strip()

    linha_llm = m.group("line_paren") or m.group("line_plain")
    return {
        **base,
        "prefixo": m.group("prefix"),
        "severidade": m.group("sev").upper(),
        "simbolo": m.group("symbol"),
        "linha_llm": int(linha_llm) if linha_llm else None,
        "descricao": body,
        "trigger": trigger,
    }


def _corrigir_linha_achado(achado: dict[str, Any], indice: dict[str, int]) -> dict[str, Any]:
    """Prefere linha real da definição do símbolo no código migrado."""
    simbolo = achado.get("simbolo")
    linha_llm = achado.get("linha_llm")
    linha = linha_llm

    if simbolo and simbolo in indice:
        linha_real = indice[simbolo]
        if linha_llm is None or linha_llm != linha_real:
            achado["linha_corrigida"] = linha_llm is not None and linha_llm != linha_real
        linha = linha_real

    achado["linha"] = linha
    achado["formatado"] = _formatar_achado(achado)
    return achado


def _formatar_achado(achado: dict[str, Any]) -> str:
    prefixo = achado.get("prefixo") or "INFO"
    sev = achado.get("severidade") or "P3"
    simbolo = achado.get("simbolo")
    linha = achado.get("linha")
    desc = achado.get("descricao") or achado.get("raw", "").lstrip("- ").strip()
    trigger = achado.get("trigger", "")

    partes = [f"- [{prefixo}][{sev}]"]
    if simbolo:
        partes.append(f"`{simbolo}`")
    if linha:
        sufixo = ""
        if achado.get("linha_corrigida") and achado.get("linha_llm"):
            sufixo = f" _(modelo citou linha {achado['linha_llm']})_"
        partes.append(f"(linha {linha}){sufixo}")
    partes.append(f"— {desc}")
    if trigger:
        partes.append(f"Trigger: {trigger}")
    return " ".join(partes)


def _normalizar_achados(
    achados: list[str],
    agente: str,
    codigo_migrado: str,
    codigo_original: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    return _pipeline_achados(achados, agente, codigo_migrado, codigo_original)


def _contar_severidades(achados: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for a in achados:
        if a.get("prefixo") and a.get("severidade"):
            counts[a["severidade"]] += 1
    return counts


def _veredito(deve_reprocessar: bool, counts: Counter[str]) -> str:
    if deve_reprocessar:
        return "🔄 **REPROCESSAR** — o migration_agent deve refazer a migração"
    if counts.get("P0", 0) > 0:
        return "❌ **REPROVADO** — corrigir achados P0 antes do merge"
    if counts.get("P1", 0) > 0:
        return "⚠️ **APROVADO COM RESSALVAS** — corrigir achados P1"
    return "✅ **APROVADO**"


def _secao_por_severidade(
    titulo: str, emoji: str, sev: str, achados: list[dict[str, Any]]
) -> str:
    itens = [a for a in achados if a.get("severidade") == sev and a.get("prefixo")]
    linhas = [a["formatado"] for a in itens]
    corpo = "\n".join(linhas) if linhas else "_(nenhum achado)_"
    return f"### {emoji} {titulo} ({sev})\n\n{corpo}\n"


def _secao_agente(titulo: str, achados: list[str]) -> str:
    if not achados:
        return f"### {titulo}\n\n_(nenhum achado)_\n"
    return f"### {titulo}\n\n" + "\n".join(achados) + "\n"


def _gerar_resumo_executivo_llm(
    llm: BaseChatModel,
    achados_resumo: str,
    deve_reprocessar: bool,
    veredito: str,
) -> str:
    try:
        prompt = _render(
            "relatorio_final",
            achados_resumo=achados_resumo,
            deve_reprocessar=str(deve_reprocessar),
        )
        response = _invocar_llm(llm, prompt)
        texto = (response.content or "").strip()
        
        return texto or f"Veredito: {veredito}"
        
    except Exception as e:
        # Captura a exceção, identifica a classe do erro e loga o traceback (exc_info=True)
        # Isso diferencia perfeitamente um KeyError/TypeError interno de um erro de API/Rede.
        logger.error(
            f"Falha ao gerar resumo executivo. Erro ({type(e).__name__}): {str(e)}", 
            exc_info=True
        )
        
        # Fallback elegante: entrega o veredito, mas avisa o desenvolvedor que lê o PR
        # que o sumário faltou por instabilidade da infraestrutura, e não por falta de achados.
        fallback_msg = (
            f"{veredito}\n\n"
            f"> ⚠️ **Aviso de Sistema:** O resumo executivo não pôde ser gerado "
            f"devido a uma falha na inferência do modelo ({type(e).__name__})."
        )
        return fallback_msg


def _gerar_relatorio_markdown(
    state: CodeReviewState,
    resumo_executivo: str,
    todos_est: list[dict[str, Any]],
    sem_fmt: list[str],
    seg_fmt: list[str],
    lint_fmt: list[str],
) -> str:
    codigo_migrado = state["codigo_migrado"]
    deve_reprocessar = bool(state.get("deve_reprocessar"))

    todos = todos_est
    counts = _contar_severidades(todos)
    veredito = _veredito(deve_reprocessar, counts)
    iteracoes = state.get("iteracao", 0)
    agentes = ", ".join(state.get("agentes_acionados", [])) or "—"
    diff = state.get("diff_estruturado", {})
    impacto = diff.get("impact_summary", "—") if isinstance(diff, dict) else "—"

    partes: list[str] = [
        "# Relatório de Revisão — Migração de Código",
        "",
        "## Legenda de severidade",
        "",
        _LEGENDA_SEVERIDADE,
        "",
        "---",
        "",
        "## 1. Resumo executivo",
        "",
        resumo_executivo,
        "",
        "## 2. Veredito",
        "",
        veredito,
        "",
        f"- **Iterações do reflection loop:** {iteracoes}",
        f"- **Agentes acionados:** {agentes}",
        f"- **Impacto (parser):** {impacto}",
        "",
        "---",
        "",
        "## 3. Achados por severidade",
        "",
        _secao_por_severidade("Crítico", "🔴", "P0", todos),
        _secao_por_severidade("Alto", "🟠", "P1", todos),
        _secao_por_severidade("Médio", "🟡", "P2", todos),
        _secao_por_severidade("Baixo / Cosmético", "🟢", "P3", todos),
        "---",
        "",
        "## 4. Detalhamento por agente",
        "",
        _secao_agente("Semântica", sem_fmt),
        _secao_agente("Segurança", seg_fmt),
        _secao_agente("Lint / Style", lint_fmt),
        "---",
        "",
        "## 5. Recomendações prioritárias",
        "",
    ]

    recs: list[str] = []
    if counts.get("P0"):
        recs.append(f"1. Corrigir **{counts['P0']}** achado(s) **P0** (bloqueadores de runtime).")
    if counts.get("P1"):
        recs.append(f"{len(recs)+1}. Revisar **{counts['P1']}** achado(s) **P1** (mudança silenciosa de comportamento).")
    if counts.get("P2"):
        recs.append(f"{len(recs)+1}. Avaliar **{counts['P2']}** achado(s) **P2** (qualidade/robustez).")
    if deve_reprocessar:
        recs.append(f"{len(recs)+1}. **Reexecutar migration_agent** antes de nova revisão.")
    if not recs:
        recs.append("Nenhuma ação bloqueante identificada.")

    partes.append("\n".join(recs))
    partes.extend(["", "---", "", "## 6. Notas sobre localização de linhas", ""])
    partes.append(
        "Linhas foram **corrigidas automaticamente** quando o achado cita um símbolo "
        "(`função`) — usa-se a linha da definição `def` no código migrado. "
        "Quando o modelo citou linha diferente, aparece _(modelo citou linha N)_."
    )

    if deve_reprocessar:
        aviso = (
            "> ⚠️ **AÇÃO REQUERIDA:** o no_critico esgotou 3 iterações com problemas "
            "críticos pendentes. O **migration_agent** deve refazer a migração.\n\n"
        )
        return aviso + "\n".join(partes)

    return "\n".join(partes)


def _achados_estruturados(state: CodeReviewState) -> list[dict[str, Any]]:
    codigo = state["codigo_migrado"]
    codigo_original = state["codigo_original"]
    resultado: list[dict[str, Any]] = []
    for agente, chave in (
        ("semantica", "achados_semantica"),
        ("seguranca", "achados_seguranca"),
        ("lint", "achados_lint"),
    ):
        _, est = _pipeline_achados(
            state.get(chave, []), agente, codigo, codigo_original
        )
        resultado.extend(est)
    return _mesclar_achados_deterministicos(
        codigo_original, codigo, resultado
    )


def _consolidar_achados(state: CodeReviewState) -> tuple[
    list[str], list[str], list[str], list[dict[str, Any]]
]:
    """Pipeline único de achados — evita re-parse no relatório."""
    codigo = state["codigo_migrado"]
    codigo_original = state["codigo_original"]
    sem_fmt, sem_est = _pipeline_achados(
        state.get("achados_semantica", []), "semantica", codigo, codigo_original
    )
    seg_fmt, seg_est = _pipeline_achados(
        state.get("achados_seguranca", []), "seguranca", codigo, codigo_original
    )
    lint_fmt, lint_est = _pipeline_achados(
        state.get("achados_lint", []), "lint", codigo, codigo_original
    )
    todos = sem_est + seg_est + lint_est
    _mesclar_achados_deterministicos(
        codigo_original, codigo, todos, sem_fmt
    )
    return sem_fmt, seg_fmt, lint_fmt, todos


def no_relatorio_final(state: CodeReviewState) -> dict:
    """
    Consolida achados em Markdown com template determinístico (seções 1–6).
    O LLM gera apenas o resumo executivo (2–3 frases); linhas são corrigidas
    via índice de símbolos do código migrado.
    """
    llm = _get_llm()
    sem_fmt, seg_fmt, lint_fmt, todos = _consolidar_achados(state)

    achados_resumo = "\n".join([
        "### Semântica:", *(sem_fmt or ["(nenhum)"]), "",
        "### Segurança:", *(seg_fmt or ["(nenhum)"]), "",
        "### Lint:", *(lint_fmt or ["(nenhum)"]),
    ])
    counts = _contar_severidades(todos)
    tem_bloqueador = counts.get("P0", 0) > 0 or counts.get("P1", 0) > 0
    deve_reprocessar = bool(state.get("deve_reprocessar")) or tem_bloqueador
    state_relatorio = {**state, "deve_reprocessar": deve_reprocessar}
    veredito = _veredito(deve_reprocessar, counts)

    resumo = _gerar_resumo_executivo_llm(
        llm, achados_resumo, deve_reprocessar, veredito
    )
    relatorio = _gerar_relatorio_markdown(
        state_relatorio, resumo, todos, sem_fmt, seg_fmt, lint_fmt
    )

    return {
        "relatorio_final": relatorio,
        "achados_semantica": sem_fmt,
        "achados_seguranca": seg_fmt,
        "achados_lint": lint_fmt,
        "deve_reprocessar": deve_reprocessar,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Montagem do Grafo
# ─────────────────────────────────────────────────────────────────────────────

def _build_graph() -> StateGraph:
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
    }
    result = graph.invoke(initial_state)
    estado_final: CodeReviewState = {**initial_state, **result}
    estruturados = _achados_estruturados(estado_final)
    return {
        "raw_diff":          result.get("raw_diff", ""),
        "diff":              result.get("diff_estruturado", {}),
        "agentes_acionados": result.get("agentes_acionados", []),
        "achados_semantica": result.get("achados_semantica", []),
        "achados_seguranca": result.get("achados_seguranca", []),
        "achados_lint":      result.get("achados_lint", []),
        "achados_estruturados": estruturados,
        "iteracoes":         result.get("iteracao", 0),
        "deve_reprocessar":  result.get("deve_reprocessar", False),
        "relatorio_final":   result.get("relatorio_final", ""),
    }


@app.post("/review", summary="Revisão via JSON (strings)")
def review_code(request: CodeReviewRequest):
    """
    Recebe os dois trechos de código como strings JSON.
    Ideal para integração programática.
    """
    return _executar_grafo(request.codigo_original, request.codigo_migrado)


def _decode_upload(raw: bytes, label: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{label}: encoding inválido — envie UTF-8 ({exc.reason})",
        ) from exc


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
    original = _decode_upload(await codigo_original.read(), "codigo_original")
    migrado = _decode_upload(await codigo_migrado.read(), "codigo_migrado")
    return _executar_grafo(original, migrado)
