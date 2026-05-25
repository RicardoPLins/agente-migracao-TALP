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
│  · Parser: extrai diff via git diff --no-index (Gemini)         │
│  · Classificador: decide quais especialistas acionar (Groq 8B)  │
│  · Fan-out paralelo: Semântica · Segurança (Gemini) · Lint      │
│  · Nó Crítico: Reflection Loop, saída antecipada por P0/P1      │
│  · Relatório final consolidado em Markdown (Groq 8B)            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Arquivo: `test_pipeline.py`

Localização: raiz do repositório (`agente-migracao-TALP/test_pipeline.py`)

### Por que este script existe

Cada agente foi desenvolvido de forma independente com seu próprio ambiente. O `test_pipeline.py` serve como **cola de integração**: importa dinamicamente cada agente via `importlib`, resolve conflitos de dependência (Ollama vs. Groq) e executa o fluxo completo a partir de um único comando.

### Decisões de implementação

#### Detecção automática de Ollama

Ao iniciar, o script testa a API REST do Ollama em `localhost:11434` (timeout 2s). Se disponível, os agentes `migration_agent` e `test_agent` usam Ollama localmente — sem custo e sem rate limit. Caso contrário, o script cai automaticamente para Groq.

```python
def _detectar_ollama() -> tuple[bool, str]:
    # testa /api/tags, retorna (disponível, modelo_detectado)
```

#### Monkey-patch de ChatOllama

O `migration_agent` e o `test_agent` foram escritos com `ChatOllama`. O script substitui esse import dinamicamente:

- **Ollama disponível** → força o modelo detectado (evita erro de modelo hardcoded diferente do instalado)
- **Ollama indisponível** → substitui `ChatOllama` por `ChatGroq`, selecionando o modelo por agente:

| Agente | Modelo Groq (fallback) | Motivo |
|---|---|---|
| migration_agent | `llama-3.1-8b-instant` | Prompts menores; preserva cota de TPM |
| test_agent | `llama-3.3-70b-versatile` | `node_analyzer` envia código completo; janela maior |

#### Estratégia multi-LLM do review_agent

O `review_agent` gerencia seus próprios modelos internamente — o pipeline apenas passa os dois arquivos:

| Nó | Modelo | Motivo |
|---|---|---|
| `no_parser` | Gemini 2.5 Flash | Recebe o diff completo (pode ter milhares de tokens) |
| `no_classificador` | Groq llama-3.1-8b-instant | Classificação simples sobre diff estruturado |
| `no_semantico` | Gemini 2.5 Flash | Raciocínio multi-step sobre equivalência funcional |
| `no_seguranca` | Gemini 2.5 Flash | Análise de domínio em segurança |
| `no_lint` | Groq llama-3.1-8b-instant | Interpreta output determinístico do Ruff |
| `no_critico` | Gemini 2.5 Flash | Meta-avaliação da qualidade dos achados (Reflection) |
| `relatorio_final` | Groq llama-3.1-8b-instant | Consolida achados já estruturados |

#### Tratamento de rate limit — dois níveis

O sistema usa duas camadas de proteção independentes:

**Nível 1 — dentro de cada nó** (`_invoke_com_retry` em `review-agent.py`):
```
Tentativa 1 → rate limit → aguarda 30s → Tentativa 2 → aguarda 60s → Tentativa 3
```
Detecta: 429, 413, `quota`, `resource_exhausted` (erros Groq e Gemini).

**Nível 2 — retry do grafo completo** (`run_review` em `test_pipeline.py`):
```
Grafo completo falha → aguarda 45s → Retry 2 → aguarda 90s → Retry 3
```
Acionado apenas se o grafo inteiro lançar exceção após esgotar os retries internos.

Erros de **limite diário (TPD)** são detectados e reportados com instrução de aguardar o reset às 00:00 UTC.

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
$env:GROQ_API_KEY   = "gsk_..."      # nós leves do review (classificador, lint, relatório)
$env:GOOGLE_API_KEY = "AIza..."      # nós pesados do review (parser, semântico, segurança, crítico)
$env:PYTHONUTF8     = "1"            # necessário para emojis nos prints (Windows)
$env:PYTHONIOENCODING = "utf-8"

# Pipeline completo (migration + test + review)
.\review_agent\.venv\Scripts\python.exe test_pipeline.py

# Apenas migration + review (pula test_agent — recomendado para testes rápidos)
.\review_agent\.venv\Scripts\python.exe test_pipeline.py --skip-test

# Apenas migration + test (pula review)
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
| `--skip-test` | off | Pula o test_agent |
| `--skip-review` | off | Pula o review_agent |
| `--ollama-model` | auto | Força um modelo Ollama específico (ex: `llama3.1`) |

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
| Review Agent | **aprovado** | 3 agentes acionados · 1 iteração · ~46s |

**Achados do Review Agent:**

- *Semântica (1):* `gzip.GzipFile(fileobj=response.content)` — recebe `bytes` em vez de `file-like` → `TypeError` garantido em runtime **[P0]**
- *Segurança:* Sem achados relevantes
- *Lint:* Nenhum novo issue introduzido pela migração
- *Veredito:* Aprovado (1 iteração — saída antecipada por ausência de achados P2/P3 adicionais)

---

## Limitações Conhecidas

### Cotas de API

| Provedor | Limite relevante | Impacto |
|---|---|---|
| Groq — llama-3.1-8b-instant | 6.000 TPM / 500.000 TPD | Nós leves do review |
| Groq — llama-3.3-70b-versatile | 6.000 TPM / 100.000 TPD | test_agent (fallback Groq) |
| Google Gemini 2.5 Flash | 1.000.000 TPM / sem TPD gratuito | Nós pesados do review |

Com Gemini nos nós pesados, os erros 429 do review são praticamente eliminados no free tier.

### Ollama (migration_agent e test_agent)

Os agentes originalmente usam Ollama local. O `test_pipeline.py` detecta automaticamente se o Ollama está disponível e o usa como prioridade. Se não estiver rodando, cai para Groq transparentemente.

---

## Dependências do `test_pipeline.py`

Todas instaladas no venv do `review_agent`:

```
langchain_groq
langchain_google_genai   # nós pesados do review_agent
langchain_ollama         # usado nativamente se Ollama disponível; patch se não
langgraph
python-dotenv
openpyxl                 # leitura do dataset do migration_agent
langchain_openai         # importado pelo test_agent (não usado diretamente)
pytest
pytest-cov
```
