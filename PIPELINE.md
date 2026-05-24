# Pipeline de Integração — migration_agent → test_agent → review_agent

Este documento descreve o script `test_pipeline.py` que orquestra os três agentes do projeto em sequência, validando o fluxo completo de migração automatizada de código.

---

## Visão Geral

O pipeline executa três etapas em ordem:

```
url.py (código urllib)
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ ETAPA 1 — Migration Agent                                       │
│  · Carrega exemplos do dataset (few-shot)                       │
│  · Infere semanticamente o comportamento do código original     │
│  · Migra urllib → requests via LLM                              │
│  · Valida o código migrado (4 critérios heurísticos)            │
└──────────────────────────────┬──────────────────────────────────┘
                               │ codigo_migrado
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ ETAPA 2 — Test Agent                                            │
│  · Analisa ambas as versões do código (Analyzer)                │
│  · Gera casos de teste de equivalência funcional (Generator)    │
│  · Executa os testes com pytest + cobertura (Executor)          │
│  · Avalia cobertura ≥ 80% e equivalência ≥ 90% (Evaluator)     │
│  · Gera relatório de equivalência em Markdown (Report)          │
└──────────────────────────────┬──────────────────────────────────┘
                               │ original + migrado
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ ETAPA 3 — Review Agent                                          │
│  · Parser: extrai diff via git diff --no-index                  │
│  · Classificador: decide quais especialistas acionar            │
│  · Fan-out paralelo: Semântica · Segurança · Lint (Ruff)        │
│  · Nó Crítico (Reflection Loop, max 3 iterações)                │
│  · Relatório final consolidado em Markdown                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Arquivo: `test_pipeline.py`

Localização: raiz do repositório (`agente-migracao-TALP/test_pipeline.py`)

### Por que este script existe

Cada agente foi desenvolvido de forma independente com seu próprio ambiente. O `test_pipeline.py` serve como **cola de integração**: importa dinamicamente cada agente via `importlib`, resolve conflitos de dependência (Ollama vs. Groq) e executa o fluxo completo a partir de um único comando.

### Decisões de implementação

#### Monkey-patch de ChatOllama → ChatGroq

O `migration_agent` e o `test_agent` foram escritos com `ChatOllama` (Ollama local). Como Ollama não é um requisito universal do projeto, o script cria um módulo `langchain_ollama` sintético antes de importar os agentes:

```python
import types, langchain_groq, sys

_fake_ollama_mod = types.ModuleType("langchain_ollama")
_fake_ollama_mod.ChatOllama = _make_groq_compat
sys.modules.setdefault("langchain_ollama", _fake_ollama_mod)
```

Dois modelos são usados dependendo do agente:

| Agente | Modelo | Motivo |
|---|---|---|
| migration_agent | `llama-3.1-8b-instant` | Prompts menores (inferência + migração); preserva cota de TPM |
| test_agent | `llama-3.3-70b-versatile` | `node_analyzer` envia código completo; precisa de janela maior |
| review_agent | `llama-3.3-70b-versatile` | Padrão hardcoded no próprio agente (sem patch) |

#### Tratamento de rate limit Groq

O review_agent implementa retry automático com backoff crescente para erros 429 por minuto:

```
Tentativa 1 → aguarda 45s → Tentativa 2 → aguarda 90s → Tentativa 3
```

Erros de **limite diário (TPD)** são detectados separadamente e reportados com instrução de aguardar o reset às 00:00 UTC.

#### Importação dinâmica dos agentes

Cada agente é importado via `importlib.util.spec_from_file_location` para evitar conflitos de nome de módulo e permitir que o script funcione sem instalar os agentes como pacotes:

```python
spec = importlib.util.spec_from_file_location("langgraph_mig03", MIGRATION_DIR / "langgraph-mig03.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
```

---

## Uso

```powershell
# Configurar variáveis de ambiente (Windows PowerShell)
$env:GROQ_API_KEY = "gsk_..."
$env:PYTHONUTF8   = "1"          # necessário para emojis nos prints (Windows)
$env:PYTHONIOENCODING = "utf-8"

# Pipeline completo (migration + test + review)
.\review_agent\.venv\Scripts\python.exe test_pipeline.py

# Apenas migration + review (pula test_agent)
.\review_agent\.venv\Scripts\python.exe test_pipeline.py --skip-test

# Apenas migration + test (pula review — útil quando cota diária Groq esgotada)
.\review_agent\.venv\Scripts\python.exe test_pipeline.py --skip-review

# Usar arquivo de entrada diferente de url.py
.\review_agent\.venv\Scripts\python.exe test_pipeline.py --input meu_codigo.py

# Controlar número de exemplos few-shot da migration (padrão: 10)
.\review_agent\.venv\Scripts\python.exe test_pipeline.py --examples 5
```

### Parâmetros

| Flag | Padrão | Descrição |
|---|---|---|
| `--input` | `url.py` | Arquivo Python urllib de entrada |
| `--output-dir` | `.pipeline_output/` | Diretório para artefatos de saída |
| `--examples` | `10` | Exemplos few-shot para o migration_agent |
| `--skip-test` | off | Pula o test_agent (economiza ~50% dos tokens) |
| `--skip-review` | off | Pula o review_agent (útil com cota diária esgotada) |

---

## Artefatos de Saída

Todos salvos em `.pipeline_output/` (criado automaticamente):

| Arquivo | Conteúdo |
|---|---|
| `migrated_code.py` | Código Python migrado para `requests` |
| `migration_result.json` | Status, tempo e mensagens do migration_agent |
| `test_report.md` | Relatório de equivalência funcional (test_agent) |
| `test_result.json` | Métricas de cobertura e equivalência |
| `review_report.md` | Relatório de revisão completo em Markdown |
| `review_result.json` | Achados brutos por categoria (semântica/segurança/lint) |
| `pipeline_summary.json` | Sumário de todas as etapas com tempos e status |

---

## Resultado do Teste de Integração (24 mai 2026)

Executado sobre `url.py` (225 linhas, `ConversationScraper` com urllib):

| Etapa | Status | Detalhe |
|---|---|---|
| Migration | **validado** | 222 linhas geradas · 4/4 critérios · ~24s |
| Test Agent | erro 413 | Prompt do `node_analyzer` excedeu TPM do modelo 8B na cota free |
| Review Agent | **aprovado** | 3 agentes acionados · 10 achados · ~174s |

**Achados do Review Agent:**

- *Semântica (4):* mudanças no comportamento de erros HTTP, encoding de dados no `urllib.parse.urlencode`, compatibilidade retroativa
- *Segurança (5):* biblioteca `requests` sem versão fixada, inputs sem validação, headers sensíveis sem redação, uso de `os.system()`
- *Lint (1):* nenhum issue novo introduzido pela migração
- *Veredito:* **Aprovado com Ressalvas**

---

## Limitações Conhecidas

### Cota Groq (free tier)

| Limite | Valor | Impacto |
|---|---|---|
| Tokens por minuto (TPM) — 70B | 12.000 | Pipeline pode pausar entre etapas |
| Tokens por minuto (TPM) — 8B | 6.000 | `node_analyzer` falha se prompt > 6.000 tokens |
| Tokens por dia (TPD) | 100.000 | Pipeline completo usa ~25-50k tokens |

**Solução:** Upgrade para [Dev Tier](https://console.groq.com/settings/billing) ou aguardar reset diário às 00:00 UTC.

### Ollama (migration_agent e test_agent)

Os agentes originalmente usam Ollama local. O monkey-patch do `test_pipeline.py` redireciona para Groq automaticamente. Para usar Ollama de fato, instale-o e remova o bloco de patch do script.

---

## Dependências do `test_pipeline.py`

Todas instaladas no venv do `review_agent`:

```
langchain_groq
langchain_ollama   # instalado mesmo sem Ollama (patch usa só o import)
langgraph
python-dotenv
openpyxl           # leitura do dataset do migration_agent
langchain_openai   # importado pelo test_agent (não usado diretamente)
pytest
pytest-cov
```
