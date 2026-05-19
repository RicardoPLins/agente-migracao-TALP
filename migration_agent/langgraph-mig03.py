"""
LLM-Powered Migration Agent - urllib to requests

This agent uses LangGraph with an LLM (Claude) to intelligently migrate
urllib code to requests. It learns from more real-world examples from the dataset
to understand migration patterns and context.

Architecture:
1. Load a larger set of examples from dataset
2. Create few-shot training prompt
3. Build agent with conditional routing
4. Process user's urllib code through migration pipeline
5. Return optimized requests code
"""

from typing import Annotated, Literal, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
import openpyxl
import os
import json
import re
from pathlib import Path
from dotenv import load_dotenv


load_dotenv()
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)
# API_KEY = os.getenv("API_KEY")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
URL_MIGRATE_PATH = PROJECT_ROOT / "url-migrate.py"
INFERENCE_JSON_PATH = PROJECT_ROOT / "inferencia.json"

# When this file is imported (e.g., by the API gateway), avoid writing files.
WRITE_ARTIFACTS = __name__ == "__main__"
# =============================================================================
# DATASET LOADING & TRAINING EXAMPLES
# =============================================================================

def carregar_exemplos_treino(num_exemplos: int = 20) -> list[dict]:
    """
    Load first N examples from dataset for few-shot learning.
    
    Args:
        num_exemplos: Number of examples to load (default 20)
    
    Returns:
        List of example dictionaries with before/after code
    """
    dataset_path = "dataset/Request-Urllib.xlsx"
    
    if not os.path.exists(dataset_path):
        print("⚠️ Dataset not found!")
        return []
    
    try:
        wb = openpyxl.load_workbook(dataset_path)
        ws = wb.active
        
        exemplos = []
        for row_idx in range(2, min(ws.max_row + 1, num_exemplos + 2)):
            repo_name = ws.cell(row_idx, 1).value
            file_name = ws.cell(row_idx, 3).value
            tipo = ws.cell(row_idx, 4).value
            code_before = ws.cell(row_idx, 7).value
            code_after = ws.cell(row_idx, 8).value
            
            if code_before and code_after:
                exemplos.append({
                    "repo": repo_name,
                    "file": file_name,
                    "type": tipo,
                    "before": code_before,
                    "after": code_after
                })
        
        print(f"✅ Loaded {len(exemplos)} training examples from dataset")
        return exemplos
    except Exception as e:
        print(f"❌ Error loading dataset: {e}")
        return []


def criar_prompt_treino(exemplos: list[dict]) -> str:
    """
    Create few-shot prompt with training examples.
    
    Args:
        exemplos: List of training examples
    
    Returns:
        Formatted prompt with examples
    """
    prompt = """You are an expert Python code migration specialist. Your task is to migrate Python code from urllib to requests library.

Key migration rules:
1. IMPORTS: Replace "from urllib.request import ..." with "import requests"
2. METHODS: Replace urlopen() with requests.get() or requests.post()
3. HEADERS: Replace response.getheader('name') with response.headers.get('name')
4. READING: Replace response.read().decode('utf-8') with response.text
5. EXCEPTIONS: Replace urllib.error exceptions with requests.exceptions
6. POST DATA: Convert urllib requests with data parameter to requests.post()

    Here are the real-world examples from GitHub migrations:

"""
    
    for i, exemplo in enumerate(exemplos, 1):
        prompt += f"\n{'='*80}\nExample {i}: {exemplo['repo']} ({exemplo['type']})\n{'='*80}\n"
        prompt += f"\nBEFORE (urllib):\n```python\n{exemplo['before']}\n```\n"
        prompt += f"\nAFTER (requests):\n```python\n{exemplo['after']}\n```\n"
    
    prompt += f"""

{'='*80}
Now, when you receive urllib code, migrate it following these patterns:

1. Analyze the urllib patterns in the code
2. Map each pattern to the corresponding requests equivalent
3. Preserve the code structure and functionality
4. Handle error cases appropriately
5. Return ONLY the migrated Python code without any explanation

Important: Return ONLY valid Python code, no markdown, no explanations."""
    
    return prompt


# =============================================================================
# STATE DEFINITION
# =============================================================================

class EstadoAgente(TypedDict):
    """
    State for the migration agent pipeline.
    
    Attributes:
        messages: Conversation history
        codigo_usuario: User's urllib code to migrate
        codigo_migrado: Migrated requests code
        inferencia_semantica: Semantic inference JSON string
        analise_agente: Agent's analysis of the migration
        status: Processing status
    """
    messages: Annotated[list[BaseMessage], add_messages]
    codigo_usuario: str
    codigo_migrado: str
    inferencia_semantica: str
    analise_agente: str
    status: str


# =============================================================================
# NODES
# =============================================================================

def no_receber_codigo(estado: EstadoAgente) -> dict:
    """
    Receive and validate user's urllib code.
    
    Args:
        estado: Current state
    
    Returns:
        Updated state with code validation
    """
    codigo = estado["codigo_usuario"]
    
    # Check if code contains urllib
    if "urllib" not in codigo:
        return {
            "messages": [AIMessage(content="⚠️ Nenhum código urllib detectado. Por favor, forneça código com urllib.")],
            "status": "no_urllib"
        }
    
    linhas = len(codigo.split('\n'))
    padroes = []
    if "urllib.request" in codigo:
        padroes.append("urllib.request")
    if "urllib2" in codigo:
        padroes.append("urllib2")
    if "urlopen" in codigo:
        padroes.append("urlopen")
    if "urllib.error" in codigo:
        padroes.append("urllib.error")
    
    mensagem = f"📝 Código recebido: {linhas} linhas | Padrões urllib: {', '.join(padroes)}"
    
    return {
        "messages": [AIMessage(content=mensagem)],
        "status": "codigo_recebido"
    }


def no_inferir_semantica(estado: EstadoAgente) -> dict:
    """
    Infer semantic intent and behavior preservation requirements before migration.

    Args:
        estado: Current state

    Returns:
        Updated state with semantic inference and file output
    """
    codigo_usuario = estado["codigo_usuario"]

    def inferencia_fallback(motivo: str) -> dict:
        entradas = ["URL(s) e parâmetros para requisição HTTP"]
        saidas = ["Corpo da resposta HTTP", "Status/headers quando acessados"]
        efeitos = ["Chamada de rede HTTP para serviço externo"]
        dependencias = ["urllib (código original)"]
        riscos = [
            "Mudança de tratamento de timeout/erros entre urllib e requests",
            "Diferenças na codificação/decodificação do corpo da resposta"
        ]
        regras = [
            "Preservar método HTTP e endpoint original",
            "Preservar headers e payload enviados",
            "Preservar tratamento de exceções e falhas de rede",
            "Preservar formato do conteúdo retornado ao chamador"
        ]

        if "Request(" in codigo_usuario or "data=" in codigo_usuario:
            entradas.append("Payload de requisição (quando houver POST/PUT)")
            efeitos.append("Envio de dados para API remota")
        if "getheader(" in codigo_usuario:
            saidas.append("Headers específicos da resposta")
        if ".read()" in codigo_usuario:
            saidas.append("Leitura binária do corpo com possível decode")
        if "urlopen(" in codigo_usuario:
            dependencias.append("Conectividade de rede e disponibilidade do endpoint")

        return {
            "intencao_principal": "Executar requisição HTTP e processar a resposta mantendo o fluxo funcional do código original.",
            "resumo_comportamento": "O código constrói/abre uma requisição com urllib, envia ao endpoint remoto, lê dados retornados e trata erros de comunicação.",
            "entradas": entradas,
            "saidas": saidas,
            "efeitos_colaterais": efeitos,
            "dependencias_externas": dependencias,
            "riscos_migracao": riscos,
            "regras_preservacao": regras,
            "observacao_fallback": motivo
        }

    try:
        groq_api_key = os.getenv("GROQ_API_KEY", "") or os.getenv("GROQ_KEY", "") or os.getenv("API_KEY", "")

        # model = ChatGroq(
        #     model="llama-3.3-70b-versatile",
        #     temperature=0,
        #     groq_api_key=groq_api_key,
        # )
        model = ChatOllama(
            model="llama3",
            temperature=0
        )

        prompt_inferencia = """Você é um analista de comportamento de código Python.

Sua tarefa NÃO é migrar o código, mas inferir semanticamente:
1) qual era a intenção do código original
2) como o comportamento funciona (fluxo lógico)
3) quais efeitos/garantias devem ser preservados após migração

Retorne SOMENTE JSON válido com esta estrutura:
{
  "intencao_principal": "...",
  "resumo_comportamento": "...",
  "entradas": ["..."],
  "saidas": ["..."],
  "efeitos_colaterais": ["..."],
  "dependencias_externas": ["..."],
  "riscos_migracao": ["..."],
  "regras_preservacao": ["..."]
}
"""

        resposta = model.invoke([
            SystemMessage(content=prompt_inferencia),
            HumanMessage(content=f"Analise semanticamente este código e gere o JSON solicitado:\n\n```python\n{codigo_usuario}\n```")
        ])

        conteudo = resposta if isinstance(resposta, str) else resposta.content
        conteudo = conteudo.strip()

        # Remove markdown fences se existirem
        if conteudo.startswith("```"):
            partes = conteudo.split("```")
            if len(partes) >= 2:
                conteudo = partes[1]
                if conteudo.startswith("json"):
                    conteudo = conteudo[4:]
                conteudo = conteudo.strip()

        try:
            inferencia_dict = json.loads(conteudo)
        except Exception:
            json_match = re.search(r"\{[\s\S]*\}", conteudo)
            if json_match:
                inferencia_dict = json.loads(json_match.group(0))
            else:
                inferencia_dict = inferencia_fallback("LLM não retornou JSON válido; usado fallback heurístico.")

        if WRITE_ARTIFACTS:
            with open(INFERENCE_JSON_PATH, "w", encoding="utf-8") as arquivo:
                json.dump(inferencia_dict, arquivo, ensure_ascii=False, indent=2)

        return {
            "messages": [AIMessage(content=f"🧠 Inferência semântica concluída e salva em: {INFERENCE_JSON_PATH}")],
            "inferencia_semantica": json.dumps(inferencia_dict, ensure_ascii=False, indent=2),
            "status": "inferencia_pronta"
        }

    except Exception as e:
        inferencia_dict = inferencia_fallback(f"Erro no nó de inferência semântica: {str(e)}")
        if WRITE_ARTIFACTS:
            with open(INFERENCE_JSON_PATH, "w", encoding="utf-8") as arquivo:
                json.dump(inferencia_dict, arquivo, ensure_ascii=False, indent=2)

        return {
            "messages": [AIMessage(content=f"⚠️ Inferência semântica gerada via fallback e salva em: {INFERENCE_JSON_PATH}")],
            "inferencia_semantica": json.dumps(inferencia_dict, ensure_ascii=False, indent=2),
            "status": "inferencia_pronta"
        }


def no_migrar_com_llm(estado: EstadoAgente, exemplos_treino: list[dict], prompt_sistema: str) -> dict:
    """
    Use LLM (Groq) to intelligently migrate code.
    
    Args:
        estado: Current state
        exemplos_treino: Training examples for few-shot learning
        prompt_sistema: System prompt with training examples
    
    Returns:
        Updated state with migrated code
    """
    codigo_usuario = estado["codigo_usuario"]
    
    try:
        # Use local Ollama model for migration (no rate limits, free)
        model = ChatOllama(
            model="llama3",
            temperature=0
        )
        
        # Create messages
        messages = [
            SystemMessage(content=prompt_sistema),
            HumanMessage(content=f"""Migrate this urllib code to requests:

```python
{codigo_usuario}
```

Return ONLY the migrated Python code without any explanation or markdown.""")
        ]
        
        # Get migration from LLM
        response = model.invoke(messages)
        codigo_migrado = response if isinstance(response, str) else response.content
        
        # Clean up markdown if present
        if codigo_migrado.startswith("```"):
            codigo_migrado = codigo_migrado.split("```")[1]
            if codigo_migrado.startswith("python"):
                codigo_migrado = codigo_migrado[6:]
            codigo_migrado = codigo_migrado.strip()
        
        mensagem = "🔄 Migração concluída com sucesso usando Ollama (local)"
        
        return {
            "messages": [AIMessage(content=mensagem)],
            "codigo_migrado": codigo_migrado,
            "analise_agente": f"Groq migration using {len(exemplos_treino)} training examples",
            "status": "migrado"
        }
    
    except Exception as e:
        erro_msg = f"❌ Erro na migração: {str(e)}"
        return {
            "messages": [AIMessage(content=erro_msg)],
            "status": "erro"
        }


def no_validar_migracao(estado: EstadoAgente) -> dict:
    """
    Validate the migrated code.
    
    Args:
        estado: Current state
    
    Returns:
        Validation results
    """
    codigo_migrado = estado.get("codigo_migrado", "")

    codigo_lower = codigo_migrado.lower()
    contem_urllib_legado = any(
        token in codigo_lower
        for token in [
            "urllib.request",
            "urllib2",
            "urllib.error",
            "urlopen(",
        ]
    )
    
    validacoes = {
        "Sem urllib legado (request/error/urlopen)": not contem_urllib_legado,
        "Com import requests": "import requests" in codigo_migrado or "requests." in codigo_migrado,
        "Sem urlopen direto": "urlopen(" not in codigo_migrado,
        "Código não vazio": len(codigo_migrado) > 0,
    }
    
    validacoes_ok = sum(1 for v in validacoes.values() if v)
    total = len(validacoes)
    
    resultado = f"🔍 Validação: {validacoes_ok}/{total} critérios atendidos\n"
    for validacao, passou in validacoes.items():
        status = "✓" if passou else "✗"
        resultado += f"  {status} {validacao}\n"
    
    return {
        "messages": [AIMessage(content=resultado)],
        "status": "validado"
    }


# =============================================================================
# CONDITIONAL EDGE
# =============================================================================

def decidir_proxima_etapa(estado: EstadoAgente) -> Literal["inferir", "migrar", "validar", "fim"]:
    """
    Decide next step based on current status.
    
    Args:
        estado: Current state
    
    Returns:
        Next node name
    """
    status = estado.get("status", "")
    
    if status == "no_urllib":
        return "fim"
    elif status == "codigo_recebido":
        return "inferir"
    elif status == "inferencia_pronta":
        return "migrar"
    elif status == "migrado":
        return "validar"
    else:
        return "fim"


# =============================================================================
# GRAPH CONSTRUCTION
# =============================================================================

def criar_agente_migracao(exemplos_treino: list[dict], prompt_sistema: str):
    """
    Build the migration agent graph.
    
    Args:
        exemplos_treino: Training examples
        prompt_sistema: System prompt with examples
    
    Returns:
        Compiled graph
    """
    grafo = StateGraph(EstadoAgente)
    
    # Add nodes
    grafo.add_node("receber", no_receber_codigo)
    grafo.add_node("inferir", no_inferir_semantica)
    grafo.add_node("migrar", lambda estado: no_migrar_com_llm(estado, exemplos_treino, prompt_sistema))
    grafo.add_node("validar", no_validar_migracao)
    
    # Add edges
    grafo.add_edge(START, "receber")
    grafo.add_conditional_edges(
        "receber",
        decidir_proxima_etapa,
        {
            "inferir": "inferir",
            "migrar": "migrar",
            "validar": "validar",
            "fim": END
        }
    )
    grafo.add_conditional_edges(
        "inferir",
        decidir_proxima_etapa,
        {
            "migrar": "migrar",
            "fim": END
        }
    )
    grafo.add_conditional_edges(
        "migrar",
        decidir_proxima_etapa,
        {
            "validar": "validar",
            "fim": END
        }
    )
    grafo.add_edge("validar", END)
    
    return grafo.compile()


# =============================================================================
# MAIN EXECUTION
# =============================================================================

if __name__ == "__main__":
    print("=" * 80)
    print("🤖 AGENTE DE MIGRAÇÃO COM IA - urllib → requests")
    print("=" * 80)
    
    # Load training examples
    print("\n📚 Carregando exemplos de treinamento...")
    exemplos_treino = carregar_exemplos_treino(30)
    
    if not exemplos_treino:
        print("❌ Não foi possível carregar exemplos de treinamento!")
        exit(1)
    
    # Create training prompt
    print("🧠 Criando prompt de treinamento com IA...")
    prompt_sistema = criar_prompt_treino(exemplos_treino)
    
    # Create agent
    print("🔧 Construindo agente de migração...")
    agente = criar_agente_migracao(exemplos_treino, prompt_sistema)

    print("\n" + "=" * 80)
    print("📝 LENDO CÓDIGO DE url.py")
    print("=" * 80)
    
    # Read urllib code from url.py
    try:
        url_file_path = os.path.join(os.path.dirname(__file__), "..", "url.py")
        with open(url_file_path, "r") as f:
            codigo_usuario = f.read().strip()
    except FileNotFoundError:
        print(f"❌ Arquivo url.py não encontrado em {url_file_path}")
        raise SystemExit(1)
    except Exception as e:
        print(f"❌ Erro ao ler url.py: {e}")
        raise SystemExit(1)

    if not codigo_usuario:
        print("❌ Arquivo url.py está vazio. Encerrando.")
        raise SystemExit(1)

    print(f"✅ Código lido com sucesso ({len(codigo_usuario.split(chr(10)))} linhas)")
    print("\n⚙️ PROCESSANDO COM AGENTE IA...\n")
    
    # Run agent
    resultado = agente.invoke({
        "messages": [HumanMessage(content="Migrar código urllib para requests")],
        "codigo_usuario": codigo_usuario,
        "codigo_migrado": "",
        "inferencia_semantica": "",
        "analise_agente": "",
        "status": ""
    })
    
    # Display results
    print("\n📊 LOG DE PROCESSAMENTO:")
    print("=" * 80)
    for msg in resultado["messages"]:
        if isinstance(msg, AIMessage):
            print(msg.content)
    
    print("\n" + "=" * 80)
    print("✅ CÓDIGO MIGRADO (REQUESTS):")
    print("=" * 80)
    codigo_final = resultado.get("codigo_migrado", "")
    if not codigo_final.strip():
        print("❌ Migração não gerou código. Arquivo `url-migrate.py` não será sobrescrito.")
        raise SystemExit(1)

    print(codigo_final[:700] + "..." if len(codigo_final) > 700 else codigo_final)

    inferencia_path = os.path.join(os.path.dirname(__file__), "..", "inferência.json")
    if os.path.exists(inferencia_path):
        print(f"\n🧠 Inferência semântica salva em: {inferencia_path}")

    output_path = URL_MIGRATE_PATH
    try:
        with open(output_path, "w", encoding="utf-8") as arquivo_saida:
            arquivo_saida.write(codigo_final.strip() + "\n")
        print(f"\n💾 Migrated code saved to: {output_path}")
    except Exception as e:
        print(f"❌ Failed to write migrated file: {e}")
    
    print("\n" + "=" * 80)
    print("📚 INFORMAÇÕES DO TREINAMENTO:")
    print("=" * 80)
    print(f"Exemplos carregados: {len(exemplos_treino)}")
    print(f"Modelo: Groq - Llama 3.1 8B Instant")
    print(f"Primeiro exemplo: {exemplos_treino[0]['repo']}")
    print(f"Tipo: {exemplos_treino[0]['type']}")
    print("=" * 80)
