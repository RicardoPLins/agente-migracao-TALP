# Pipeline de Integração — migration_agent → test_agent → review_agent

Este documento descreve o script `test_pipeline.py`, que orquestra os três agentes em sequência. Para instalação, chaves de API e replicação em Linux/macOS/Windows, consulte **[REPLICACAO.md](REPLICACAO.md)**.

---

## Visão geral

```
url.py (código urllib)
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ ETAPA 1 — Migration Agent                                       │
│  receber → migrar → validar                                     │
│  · Few-shot a partir de dataset/Request-Urllib.xlsx             │
│  · Migra urllib → requests via LLM                              │
│  · Valida heurísticas básicas do código gerado                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ codigo_migrado
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ ETAPA 2 — Test Agent  (loop interno até 3× se NEEDS_REVISION)   │
│  analyzer → inspector → generator → executor →                  │
│  evaluator → router → report                                    │
│  · Gera e executa testes de equivalência (pytest + cov)        │
│  · Cobertura ≥ 80% · Equivalência ≥ 90%                        │
└──────────────────────────────┬──────────────────────────────────┘
                               │ original + migrado
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ ETAPA 3 — Review Agent                                          │
│  parser → classificador → roteador ──► semantico  ─┐           │
│                                       seguranca  ─┤           │
│                                       lint       ─┘           │
│                                            critico → relatorio  │
│  · Diff via git diff --no-index + especialistas em paralelo    │
│  · Reflection loop no nó crítico (até 3 iterações)            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Arquivo: `test_pipeline.py`

**Localização:** raiz do repositório (`agente-migracao-TALP/test_pipeline.py`)

### Por que este script existe

Cada agente foi desenvolvido de forma independente. O `test_pipeline.py` funciona como **cola de integração**: importa cada agente via `importlib`, detecta Ollama/Groq, executa o fluxo completo e grava artefatos em `.pipeline_output/`.

### Loop interno migration → test

Antes do review, o pipeline pode repetir migration + test até **3 vezes** (`MAX_REVISION_LOOPS = 3`) quando o `router` do test agent retorna `NEEDS_REVISION`. Nesse caso, as sugestões do router são injetadas como contexto na próxima migração.

Artefatos por iteração: `migrated_code_iter1.py`, `migration_result_iter1.json`, etc.

---

## Backends de LLM por agente

| Agente | Backend real no código | Variáveis de ambiente |
|---|---|---|
| **migration_agent** | Ollama (se disponível) ou Groq `llama-3.3-70b-versatile` | `GROQ_API_KEY` (fallback Groq) |
| **test_agent** | `ChatOpenAI` via API compatível com OpenAI | `PROVIDER_API_KEY`, `PROVIDER_BASE_URL` |
| **review_agent** | Groq `llama-3.3-70b-versatile` em todos os nós | `API_3` |

> **Inconsistência conhecida:** `test_pipeline.py` valida `GOOGLE_API_KEY` + `GROQ_API_KEY` antes do review, mas `review-agent.py` na `main` lê apenas `API_3`. Defina `API_3` com a mesma chave Groq. Use `--skip-review` se não quiser configurar `GOOGLE_API_KEY`.

> **Ollama e test agent:** o monkey-patch de `ChatOllama` afeta só o `migration_agent`. O `test_agent` usa `PROVIDER_*` diretamente — Ollama **não** substitui o test agent automaticamente.

### Detecção automática de Ollama

Ao iniciar, o script testa `OLLAMA_HOST` (padrão `http://localhost:11434`, timeout 2s). Se disponível, o **migration_agent** usa LLM local. Caso contrário, cai para Groq.

Modelos preferidos (em ordem): `llama3.1`, `llama3.2`, `llama3`, `llama3.3`. Override via `--ollama-model` ou `OLLAMA_MODEL`.

### Monkey-patch de ChatOllama (somente migration)

Quando Ollama **não** está disponível, o script substitui `ChatOllama` por `ChatGroq` antes de importar o migration agent:

| Contexto | Modelo Groq |
|---|---|
| migration_agent | `llama-3.1-8b-instant` (via stack trace no patch) |
| migration_agent direto (`langgraph-mig03.py`) | `llama-3.3-70b-versatile` |

O `test_agent` **não** passa pelo patch — requer `PROVIDER_API_KEY` + `PROVIDER_BASE_URL` (ex.: Groq OpenAI endpoint).

### Review agent — modelos

Na `main`, todos os nós (`parser`, `classificador`, `semantico`, `seguranca`, `lint`, `critico`, `relatorio_final`) usam um único LLM:

```python
ChatGroq(api_key=os.getenv("API_3"), model_name="llama-3.3-70b-versatile")
```

O `test_pipeline.py` ainda exibe "Gemini Pro/Flash + Groq" nos logs quando Ollama está off — isso reflete documentação/comentários antigos, não o código atual do review.

### Rate limit

**Retry externo** (`run_review` em `test_pipeline.py`): até 3 tentativas do grafo completo com backoff de 45s × tentativa em erros 429. Limites diários (TPD) abortam com mensagem para aguardar reset às 00:00 UTC.

Pausas fixas entre etapas quando Groq está ativo: 10s antes do test, 15s entre iterações do loop, 10s antes do review.

---

## Variáveis de ambiente

Crie `.env` na raiz (detalhes em [REPLICACAO.md](REPLICACAO.md#5-configurar-chaves-de-api-e-modelos)):

```env
GROQ_API_KEY=gsk_...
API_3=gsk_...                              # review_agent (mesma chave Groq)
PROVIDER_API_KEY=gsk_...
PROVIDER_BASE_URL=https://api.groq.com/openai/v1
GOOGLE_API_KEY=AIza...                     # validada pelo test_pipeline se review rodar
```

Opcionais: `OLLAMA_HOST`, `OLLAMA_MODEL`, `REVIEW_OLLAMA_MODEL_HEAVY`, `REVIEW_OLLAMA_MODEL_LIGHT`.

---

## Uso

### Linux / macOS

```bash
source .venv/bin/activate
PYTHONUTF8=1 python test_pipeline.py
```

### Windows (PowerShell)

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
python test_pipeline.py
```

### Exemplos

```bash
# Pipeline completo
python test_pipeline.py

# Pula test (mais rápido)
python test_pipeline.py --skip-test

# Pula review (útil com cota esgotada)
python test_pipeline.py --skip-review

# Entrada customizada
python test_pipeline.py --input meu_codigo.py

# Menos exemplos few-shot (padrão: 30)
python test_pipeline.py --examples 10

# Forçar modelo Ollama
python test_pipeline.py --ollama-model llama3.1
```

### Parâmetros

| Flag | Padrão | Descrição |
|---|---|---|
| `--input` | `url.py` | Arquivo Python urllib de entrada |
| `--output-dir` | `.pipeline_output/` | Diretório de artefatos |
| `--examples` | `30` | Exemplos few-shot do migration_agent |
| `--skip-test` | off | Pula o test_agent |
| `--skip-review` | off | Pula o review_agent |
| `--ollama-model` | auto | Força modelo Ollama (ex.: `llama3.1`) |

---

## Artefatos de saída

Salvos em `.pipeline_output/`:

| Arquivo | Conteúdo |
|---|---|
| `migrated_code.py` | Código migrado final |
| `migrated_code_iterN.py` | Código por iteração do loop (se houver) |
| `migration_result.json` | Status, tempo e mensagens do migration |
| `test_report.md` | Relatório de equivalência (test_agent) |
| `test_result.json` | Métricas, router decision, cobertura |
| `review_report.md` | Relatório de revisão em Markdown |
| `review_result.json` | Achados brutos por categoria |
| `pipeline_summary.json` | Sumário de todas as etapas |

---

## Importação dinâmica dos agentes

Cada agente é carregado via `importlib.util.spec_from_file_location` para evitar conflitos de módulo:

```python
spec = importlib.util.spec_from_file_location("langgraph_mig03", MIGRATION_DIR / "langgraph-mig03.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
```

Paths: `migration_agent/langgraph-mig03.py`, `test_agent/agent/agent.py`, `review_agent/review-agent.py`.

---

## Limitações conhecidas

### Cotas Groq (free tier)

| Modelo | TPM | TPD |
|---|---|---|
| `llama-3.1-8b-instant` | 6.000 | 500.000 |
| `llama-3.3-70b-versatile` | 6.000 | 100.000 |

O review e o migration (fallback) usam o 70B — monitorar TPD.

### Test agent sem PROVIDER_*

Sem `PROVIDER_API_KEY` e `PROVIDER_BASE_URL`, o test agent falha mesmo com Ollama rodando.

### Review exige API_3

Erro `API_3 não encontrada` se a variável não estiver no `.env`, independentemente de `GROQ_API_KEY`.

---

## Dependências

Instale na raiz (venv `.venv/`):

```bash
pip install -r requirements.txt
pip install -r review_agent/requirements.txt
```

Principais pacotes: `langgraph`, `langchain-groq`, `langchain-openai`, `langchain-ollama`, `python-dotenv`, `openpyxl`, `pytest`, `pytest-cov`, `responses`, `ruff`.

---

## Documentação relacionada

| Arquivo | Conteúdo |
|---|---|
| [REPLICACAO.md](REPLICACAO.md) | Guia completo de replicação |
| [README.md](README.md) | Visão geral e fluxo de cada agente |
| [review_agent/README.md](review_agent/README.md) | API FastAPI e arquitetura do review |
