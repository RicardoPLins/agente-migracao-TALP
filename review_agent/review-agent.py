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
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Literal, TypedDict

from fastapi import FastAPI, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from langchain_groq import ChatGroq
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


def _get_llm() -> ChatGroq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY não encontrada nas variáveis de ambiente.")
    return ChatGroq(
        api_key=api_key,
        model_name="llama-3.3-70b-versatile",
        temperature=0.0,
    )


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
    response = llm.invoke(prompt)
    try:
        diff = json.loads(_strip_md_fences(response.content))
    except json.JSONDecodeError:
        diff = {"raw": response.content, "parse_error": True}

    return {
        "raw_diff":        raw_diff or "",
        "diff_estruturado": diff,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Nó 2 – Classificador (Semântico Leve)
# ─────────────────────────────────────────────────────────────────────────────

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

    llm = _get_llm()
    diff_str = json.dumps(state["diff_estruturado"], ensure_ascii=False, indent=2)
    prompt = _render("classificador", diff_str=diff_str)
    response = llm.invoke(prompt)
    try:
        parsed = json.loads(_strip_md_fences(response.content))
        agentes = parsed.get("agentes", ["semantica"])
    except (json.JSONDecodeError, AttributeError):
        agentes = ["semantica", "seguranca", "lint"]

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
    prompt = _render(
        "agente_semantica",
        critica=critica,
        diff_str=diff_str,
        raw_diff=state.get("raw_diff") or "(git diff não disponível)",
        codigo_original=state["codigo_original"],
        codigo_migrado=state["codigo_migrado"],
    )
    response = llm.invoke(prompt)
    achados = [
        linha.strip()
        for linha in response.content.split("\n")
        if linha.strip().startswith("-")
    ]
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
    prompt = _render(
        "agente_seguranca",
        critica=critica,
        diff_str=diff_str,
        raw_diff=state.get("raw_diff") or "(git diff não disponível)",
        codigo_original=state["codigo_original"],
        codigo_migrado=state["codigo_migrado"],
    )
    response = llm.invoke(prompt)
    achados = [
        linha.strip()
        for linha in response.content.split("\n")
        if linha.strip().startswith("-")
    ]
    return {"achados_seguranca": achados or ["- Sem achados de segurança relevantes."]}


# ─────────────────────────────────────────────────────────────────────────────
# Nó 4c – no_lint  (tool use determinístico via Ruff + interpretação LLM)
# ─────────────────────────────────────────────────────────────────────────────

def _inferir_config_ruff(codigo_original: str, llm: ChatGroq) -> dict:
    """
    Usa o LLM para inferir o estilo implícito do código original e retorna
    uma configuração Ruff ajustada (line_length, regras, indent_width).
    """
    prompt = _render("agente_lint_config", codigo_original=codigo_original)
    response = llm.invoke(prompt)
    defaults = {"line_length": 88, "select": ["E", "W", "F", "I"], "ignore": [], "indent_width": 4}
    try:
        config = json.loads(_strip_md_fences(response.content))
        return {
            "line_length":  int(config.get("line_length", defaults["line_length"])),
            "select":       config.get("select", defaults["select"]),
            "ignore":       config.get("ignore", defaults["ignore"]),
            "indent_width": int(config.get("indent_width", defaults["indent_width"])),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
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
    response = llm.invoke(prompt)
    achados = [
        linha.strip()
        for linha in response.content.split("\n")
        if linha.strip().startswith("-")
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
    response = llm.invoke(prompt)
    try:
        resultado = json.loads(_strip_md_fences(response.content))
        decisao = resultado.get("decisao", "aprovado")
        motivo  = resultado.get("motivo_rejeicao", "")
    except json.JSONDecodeError:
        decisao = "aprovado"
        motivo  = ""

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
# Nó 6 – Relatório Final
# ─────────────────────────────────────────────────────────────────────────────

def no_relatorio_final(state: CodeReviewState) -> dict:
    """
    Consolida todos os achados validados em um relatório Markdown estruturado.
    A IA NÃO corrige o código — apenas reporta e orienta.

    Quando `deve_reprocessar = True`, antecipa um cabeçalho de alerta indicando
    que o migration_agent deve ser acionado novamente.
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
    diff_str = json.dumps(state.get("diff_estruturado", {}), ensure_ascii=False, indent=2)

    prompt = _render(
        "relatorio_final",
        achados_str=achados_str,
        diff_str=diff_str,
    )
    response = llm.invoke(prompt)

    conteudo = response.content
    if state.get("deve_reprocessar"):
        aviso = (
            "# ⚠️ AÇÃO REQUERIDA: Reprocessamento pelo Migration Agent\n\n"
            "> O no_critico avaliou **3 iterações** de refinamento e ainda identificou "
            "problemas críticos. A migração deve ser refeita pelo **migration_agent** "
            "antes de uma nova revisão.\n\n---\n\n"
        )
        conteudo = aviso + conteudo

    return {"relatorio_final": conteudo}


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
    return {
        "raw_diff":          result.get("raw_diff", ""),
        "diff":              result.get("diff_estruturado", {}),
        "agentes_acionados": result.get("agentes_acionados", []),
        "achados_semantica": result.get("achados_semantica", []),
        "achados_seguranca": result.get("achados_seguranca", []),
        "achados_lint":      result.get("achados_lint", []),
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
