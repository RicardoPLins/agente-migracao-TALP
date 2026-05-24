# Guia de Replicação — Pipeline de Migração Automatizada

Este guia detalha **exatamente** os passos necessários para replicar o experimento em uma máquina nova, do zero à execução completa do pipeline `migration_agent → test_agent → review_agent`.

---

## Sumário

1. [Pré-requisitos de sistema](#1-pré-requisitos-de-sistema)
2. [Clonar o repositório](#2-clonar-o-repositório)
3. [Criar e configurar o ambiente virtual](#3-criar-e-configurar-o-ambiente-virtual)
4. [Instalar as dependências](#4-instalar-as-dependências)
5. [Configurar a chave de API Groq](#5-configurar-a-chave-de-api-groq)
6. [Verificar os pré-requisitos opcionais](#6-verificar-os-pré-requisitos-opcionais)
7. [Executar o pipeline completo](#7-executar-o-pipeline-completo)
8. [Verificar os artefatos de saída](#8-verificar-os-artefatos-de-saída)
9. [Executar apenas o review_agent (standalone)](#9-executar-apenas-o-review_agent-standalone)
10. [Solução de problemas comuns](#10-solução-de-problemas-comuns)
11. [Estrutura completa do repositório](#11-estrutura-completa-do-repositório)

---

## 1. Pré-requisitos de sistema

| Requisito | Versão mínima | Como verificar |
|---|---|---|
| Python | 3.9+ (recomendado 3.10+) | `python --version` |
| pip | qualquer | `pip --version` |
| git | qualquer | `git --version` |
| Ruff | qualquer | `ruff --version` |

### Instalar Ruff (se não tiver)

```bash
pip install ruff
```

> **Nota:** `git` é usado pelo `no_parser` do review_agent para gerar diffs determinísticos via `git diff --no-index`. Ele não precisa estar em um repositório — basta estar no PATH.

---

## 2. Clonar o repositório

```bash
git clone <url-do-repositório>
cd agente-migracao-TALP
```

Estrutura esperada após o clone:

```
agente-migracao-TALP/
├── migration_agent/
│   └── langgraph-mig03.py
├── test_agent/
│   ├── agent/agent.py
│   └── prompts/
├── review_agent/
│   ├── review-agent.py
│   ├── prompts/
│   └── requirements.txt
├── dataset/
│   └── Request-Urllib.xlsx
├── url.py                  ← código urllib de entrada (exemplo)
├── test_pipeline.py        ← script de integração
└── REPLICACAO.md           ← este arquivo
```

---

## 3. Criar e configurar o ambiente virtual

O pipeline usa **um único ambiente virtual** localizado em `review_agent/.venv`. Ele concentra as dependências de todos os três agentes.

### Windows (PowerShell)

```powershell
cd review_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> Se aparecer erro de política de execução:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

### Linux / macOS

```bash
cd review_agent
python3 -m venv .venv
source .venv/bin/activate
```

---

## 4. Instalar as dependências

Com o venv ativo, dentro de `review_agent/`:

```bash
# Dependências principais do review_agent
pip install -r requirements.txt

# Dependências adicionais dos outros agentes (instaladas no mesmo venv)
pip install openpyxl langchain-ollama langchain-openai pytest pytest-cov responses pytest-responses
```

> **Por que langchain-ollama se não usamos Ollama?**
> O `migration_agent` e o `test_agent` importam `langchain_ollama` no nível de módulo. O `test_pipeline.py` substitui o `ChatOllama` por `ChatGroq` via monkey-patch antes do import, mas o pacote ainda precisa estar instalado para a importação não falhar.

### Verificar instalação

```bash
python -c "import langgraph, langchain_groq, langchain_ollama, openpyxl, fastapi, ruff; print('OK')"
```

Saída esperada: `OK`

---

## 5. Configurar a chave de API Groq

### Obter a chave

1. Acesse [console.groq.com](https://console.groq.com/)
2. Crie uma conta gratuita
3. Em **API Keys**, clique em **Create API Key**
4. Copie a chave (começa com `gsk_`)

### Configurar

**Opção A — arquivo `.env`** (recomendado para uso recorrente):

Crie `agente-migracao-TALP/.env`:

```env
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Opção B — variável de ambiente na sessão** (execução única):

```powershell
# Windows PowerShell
$env:GROQ_API_KEY = "gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

```bash
# Linux / macOS
export GROQ_API_KEY="gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### Limites da conta gratuita Groq

| Modelo | Tokens/min (TPM) | Tokens/dia (TPD) |
|---|---|---|
| `llama-3.3-70b-versatile` | 12.000 | 100.000 |
| `llama-3.1-8b-instant` | 6.000 | 500.000 |

> O pipeline completo consome aproximadamente **25.000–50.000 tokens por execução**. Se atingir o limite diário, aguarde o reset às **00:00 UTC** ou faça upgrade para o Dev Tier.

---

## 6. Verificar os pré-requisitos opcionais

### Verificar git no PATH

```bash
git --version
# Esperado: git version 2.x.x
```

### Verificar ruff no PATH

```bash
ruff --version
# Esperado: ruff 0.x.x
```

Se `ruff` não estiver no PATH mas estiver instalado no venv:

```bash
# O review_agent usa subprocess para chamar ruff — precisa estar no PATH global
# Alternativa: adicionar o venv ao PATH antes de rodar
export PATH="$PWD/review_agent/.venv/bin:$PATH"
```

---

## 7. Executar o pipeline completo

Retorne à raiz do repositório:

```bash
cd ..   # de volta a agente-migracao-TALP/
```

### Windows (PowerShell)

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:GROQ_API_KEY = "gsk_..."   # se não criou .env

.\review_agent\.venv\Scripts\python.exe test_pipeline.py
```

### Linux / macOS

```bash
PYTHONUTF8=1 ./review_agent/.venv/bin/python test_pipeline.py
```

> As variáveis `PYTHONUTF8` e `PYTHONIOENCODING` são necessárias no Windows para que os emojis nos prints dos agentes não causem `UnicodeEncodeError` no terminal. No Linux/macOS normalmente não são necessárias.

### Modos de execução recomendados

```bash
# Execução mais rápida: pula o test_agent (~24s total)
python test_pipeline.py --skip-test

# Execução econômica em tokens: só migration + test
python test_pipeline.py --skip-review

# Usar código próprio como entrada
python test_pipeline.py --input meu_codigo_urllib.py

# Reduzir exemplos few-shot para economizar tokens (padrão: 10)
python test_pipeline.py --examples 5 --skip-test
```

### Saída esperada no terminal

```
############################################################
  PIPELINE DE INTEGRAÇÃO
  Início: 2026-05-24 08:41:19
  Input : .../url.py
  Output: .../.pipeline_output
############################################################

============================================================
  ETAPA 1 — MIGRATION AGENT
============================================================
  Carregando 10 exemplos do dataset...
✅ Loaded 10 training examples from dataset
  Status  : validado
  Tempo   : ~24s
  Linhas migradas: 222
  > 🔍 Validação: 4/4 critérios atendidos
  ✓ Sem urllib legado (request/error/urlopen)
  ✓ Com import requests
  ✓ Sem urlopen direto
  ✓ Código não vazio

  ETAPA 2 — TEST AGENT: pulada (--skip-test)

  Aguardando 15s para respeitar rate limit do Groq entre etapas...

============================================================
  ETAPA 3 — REVIEW AGENT
============================================================
  Executando grafo de revisão (tentativa 1/3)...
  Agentes acionados : ['semantica', 'seguranca', 'lint']
  Iterações crítico : 1
  Deve reprocessar  : False
  Achados semântica : 4
  Achados segurança : 5
  Achados lint      : 1
  Tempo             : ~174s

############################################################
  PIPELINE CONCLUÍDO
  Tempo total : ~210s
  Migration   : validado
  Test        : skipped
  Review      : aprovado
  Artefatos   : .../.pipeline_output
############################################################
```

---

## 8. Verificar os artefatos de saída

Todos os artefatos ficam em `.pipeline_output/`:

```bash
ls .pipeline_output/
```

| Arquivo | O que contém |
|---|---|
| `migrated_code.py` | Código migrado de urllib para requests |
| `migration_result.json` | Status, tempo e log de mensagens do migration_agent |
| `test_report.md` | Relatório de equivalência funcional (apenas se test_agent rodou) |
| `test_result.json` | Métricas de cobertura e equivalência (apenas se test_agent rodou) |
| `review_report.md` | Relatório de revisão completo (semântica + segurança + lint + veredito) |
| `review_result.json` | Achados brutos por categoria |
| `pipeline_summary.json` | Sumário de todas as etapas com tempos e status |

### Visualizar o relatório de revisão

```bash
# Linux / macOS
cat .pipeline_output/review_report.md

# Windows PowerShell
Get-Content .pipeline_output\review_report.md
```

---

## 9. Executar apenas o review_agent (standalone)

O review_agent pode ser usado de forma independente — sem o migration_agent ou test_agent.

### Via API FastAPI

```bash
cd review_agent
.\.venv\Scripts\python.exe -m uvicorn "review-agent:app" --host 127.0.0.1 --port 8000 --reload
```

Acesse `http://127.0.0.1:8000/docs` para a interface Swagger.

### Via curl

```bash
curl -X POST http://127.0.0.1:8000/review \
  -H "Content-Type: application/json" \
  -d '{
    "codigo_original": "import urllib.request\n\ndef buscar(url):\n    with urllib.request.urlopen(url) as r:\n        return r.read().decode()\n",
    "codigo_migrado":  "import requests\n\ndef buscar(url):\n    return requests.get(url).text\n"
  }'
```

### Via upload de arquivos (Swagger UI)

1. Acesse `http://127.0.0.1:8000/docs`
2. Clique em `POST /review/files`
3. Clique em **Try it out**
4. Selecione os arquivos `.py` original e migrado
5. Clique em **Execute**

---

## 10. Solução de problemas comuns

### `ModuleNotFoundError: No module named 'langchain_ollama'`

```bash
pip install langchain-ollama
```

### `ModuleNotFoundError: No module named 'openpyxl'`

```bash
# Use o pip do próprio venv
python -m pip install openpyxl
```

### `UnicodeEncodeError: 'charmap' codec can't encode character` (Windows)

Adicione estas variáveis antes de executar:

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

### `NameError: name 'List' is not defined` (review_agent)

Ocorre com Python 3.10+ quando `List` do `typing` é usado em TypedDict e avaliado via `get_type_hints`. Já corrigido na versão atual do `review-agent.py` (usa `list[str]` nativo).

Se encontrar em outro arquivo, substitua `List[X]` por `list[X]`.

### `groq.RateLimitError: 429` (por minuto)

O `test_pipeline.py` já tenta automaticamente com backoff. Se persistir, aguarde 1–2 minutos e execute novamente com `--skip-test` para reduzir o consumo de tokens.

### `groq.RateLimitError: 429` (diário — TPD)

```
Rate limit reached ... on tokens per day (TPD): Limit 100000, Used 97385
```

Aguarde o reset às **00:00 UTC** (21:00 horário de Brasília). Enquanto isso, você pode:
- Usar `--skip-review` para rodar apenas migration + test
- Fazer upgrade para o [Dev Tier](https://console.groq.com/settings/billing)

### `Error code: 413` no test_agent (prompt muito grande)

O `node_analyzer` do test_agent envia ambos os códigos completos para o modelo. Com o modelo 8B (TPM 6.000), o prompt pode exceder o limite. O pipeline já usa automaticamente o 70B para o test_agent.

### `FileNotFoundError: Input file not found`

Verifique se `url.py` existe na raiz do repositório ou use `--input` para apontar para o arquivo correto:

```bash
python test_pipeline.py --input /caminho/para/meu_codigo.py
```

### `Dataset not found` no migration_agent

O arquivo `dataset/Request-Urllib.xlsx` precisa existir. Se não estiver presente, o agente continua sem exemplos few-shot (migração menos precisa). Verifique se o clone incluiu a pasta `dataset/`.

---

## 11. Estrutura completa do repositório

```
agente-migracao-TALP/
│
├── migration_agent/
│   └── langgraph-mig03.py       # Agente de migração urllib → requests
│
├── test_agent/
│   ├── agent/
│   │   └── agent.py             # Agente de equivalência funcional
│   ├── prompts/
│   │   ├── node1_analyzer.txt
│   │   ├── node2_generator.txt
│   │   ├── node4_evaluator.txt
│   │   └── node_report.txt
│   ├── requirements.txt
│   └── README.md
│
├── review_agent/
│   ├── prompts/
│   │   ├── parser.json
│   │   ├── classificador.json
│   │   ├── agente_semantica.json
│   │   ├── agente_seguranca.json
│   │   ├── agente_lint_config.json
│   │   ├── agente_lint_interpretacao.json
│   │   ├── no_critico.json
│   │   └── relatorio_final.json
│   ├── test1/                   # Exemplos de entrada/saída para testes manuais
│   ├── .venv/                   # Ambiente virtual (usado por todo o pipeline)
│   ├── review-agent.py          # Orquestrador LangGraph + API FastAPI
│   ├── requirements.txt
│   └── README.md
│
├── dataset/
│   └── Request-Urllib.xlsx      # Exemplos reais de migração (GitHub)
│
├── scripts/                     # Scripts alternativos de pipeline (legado)
│   ├── run_pipeline_real.py
│   └── run_pipeline_with_feedback.py
│
├── url.py                       # Código urllib de entrada padrão (ConversationScraper)
├── url-migrate.py               # Código migrado (gerado pelo migration_agent standalone)
├── inferencia.json              # Inferência semântica (gerada pelo migration_agent)
├── test_pipeline.py             # Script de integração dos três agentes
│
├── PIPELINE.md                  # Documentação do test_pipeline.py
├── REPLICACAO.md                # Este guia
└── README.md                    # README geral do projeto
```

---

## Referências

- [LangGraph — Documentação oficial](https://langchain-ai.github.io/langgraph/)
- [Groq — Console e API Keys](https://console.groq.com/)
- [Ruff — Linter Python](https://docs.astral.sh/ruff/)
- [awesome-reviewers — Skills de revisão](https://github.com/baz-scm/awesome-reviewers)
- [pr-agent (Qodo) — Referência de prompts](https://github.com/Codium-ai/pr-agent)
