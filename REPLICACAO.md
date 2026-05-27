# Guia de Replicação — Pipeline de Migração Automatizada

Este guia descreve como replicar o experimento **do zero** em uma máquina nova (Linux, macOS ou Windows), até a execução completa do pipeline `migration_agent → test_agent → review_agent`.

> **Estado atual da branch `main`:** o ponto de entrada recomendado e funcional é `test_pipeline.py` na raiz do repositório.

---

## Sumário

1. [Pré-requisitos de sistema](#1-pré-requisitos-de-sistema)
2. [Clonar o repositório](#2-clonar-o-repositório)
3. [Criar o ambiente virtual](#3-criar-o-ambiente-virtual)
4. [Instalar dependências](#4-instalar-dependências)
5. [Configurar chaves de API e modelos](#5-configurar-chaves-de-api-e-modelos)
6. [Ollama (opcional, recomendado)](#6-ollama-opcional-recomendado)
7. [Executar o pipeline completo](#7-executar-o-pipeline-completo)
8. [Artefatos de saída](#8-artefatos-de-saída)
9. [Executar agentes individualmente](#9-executar-agentes-individualmente)
10. [Solução de problemas](#10-solução-de-problemas)
11. [Estrutura do repositório](#11-estrutura-do-repositório)

---

## 1. Pré-requisitos de sistema


| Requisito | Versão mínima | Uso                                                |
| --------- | ------------- | -------------------------------------------------- |
| Python    | **3.11+**     | Todos os agentes e scripts                         |
| pip       | qualquer      | Instalação de pacotes                              |
| git       | qualquer      | `review_agent` usa `git diff --no-index` no parser |
| Ruff      | qualquer      | Nó de lint do `review_agent` (subprocess)          |


### Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
pip install ruff   # ou: sudo apt install ruff (se disponível no seu distro)
```

### macOS

```bash
# Homebrew: https://brew.sh
brew install python git ruff
```

Para LLM local (opcional):

```bash
brew install ollama
```

### Windows

1. Instale [Python 3.11+](https://www.python.org/downloads/) — marque **“Add Python to PATH”**.
2. Instale [Git for Windows](https://git-scm.com/download/win).
3. Instale Ruff:

```powershell
pip install ruff
```

### Verificar instalação

```bash
python --version    # ou: python3 --version
pip --version
git --version
ruff --version
```

---

## 2. Clonar o repositório

```bash
git clone <url-do-repositório>
cd agente-migracao-TALP
```

Estrutura principal após o clone:

```
agente-migracao-TALP/
├── migration_agent/langgraph-mig03.py   # Migração urllib → requests
├── test_agent/agent/agent.py              # Testes de equivalência funcional
├── review_agent/review-agent.py           # Revisão semântica, segurança e lint
├── dataset/Request-Urllib.xlsx            # Exemplos few-shot para migração
├── url.py                                 # Código urllib de entrada (padrão)
├── test_pipeline.py                       # Pipeline integrado (recomendado)
├── scripts/                               # Scripts alternativos (ver nota acima)
├── requirements.txt                       # Dependências da raiz
└── REPLICACAO.md                          # Este guia
```

---

## 3. Criar o ambiente virtual

Use **um único venv na raiz** (`.venv/`). Todos os agentes compartilham esse ambiente.

### Linux / macOS

```bash
cd agente-migracao-TALP
python3 -m venv .venv
source .venv/bin/activate
```

### Windows (PowerShell)

```powershell
cd agente-migracao-TALP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Se aparecer erro de política de execução:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

---

## 4. Instalar dependências

Com o venv ativo, na **raiz do repositório**:

```bash
pip install --upgrade pip
pip install -r requirements.txt
pip install -r review_agent/requirements.txt
```

O `review_agent/requirements.txt` inclui pacotes extras (`langchain-google-genai`, `ruff`, `mypy`, etc.) usados pelo agente de revisão.

### Verificar instalação

```bash
python -c "import langgraph, langchain_groq, langchain_openai, openpyxl, fastapi; print('OK')"
```

Saída esperada: `OK`

---

## 5. Configurar chaves de API e modelos

O pipeline usa **três backends de LLM** distintos. Crie um arquivo `.env` na **raiz do repositório** (não commite — já está no `.gitignore`):

```env
# ── Groq (migration fallback + review_agent) ──────────────────────────────
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
API_3=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx          # mesma chave — review_agent lê API_3

# ── Test agent (API compatível com OpenAI) ────────────────────────────────
PROVIDER_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
PROVIDER_BASE_URL=https://api.groq.com/openai/v1

# ── Validação do test_pipeline (obrigatória se review não for pulado) ───────
GOOGLE_API_KEY=AIzaxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> **Importante:** o `review_agent/review-agent.py` na `main` usa **apenas Groq** via variável `API_3` (modelo `llama-3.3-70b-versatile`). O `test_pipeline.py` ainda valida `GOOGLE_API_KEY` antes de chamar o review — defina-a mesmo que o review atual não use Gemini, ou use `--skip-review`.

### Tabela de variáveis


| Variável            | Agente                    | Obrigatória                           | Descrição                                                |
| ------------------- | ------------------------- | ------------------------------------- | -------------------------------------------------------- |
| `GROQ_API_KEY`      | migration (fallback Groq) | Sim*, se Ollama indisponível          | Chave Groq                                               |
| `API_3`             | review                    | Sim, se `--skip-review` não for usado | Mesma chave Groq — nome exigido pelo código do review    |
| `PROVIDER_API_KEY`  | test                      | Sim                                   | Chave do provedor OpenAI-compatível                      |
| `PROVIDER_BASE_URL` | test                      | Sim                                   | URL base da API (Groq: `https://api.groq.com/openai/v1`) |
| `GOOGLE_API_KEY`    | test_pipeline (validação) | Sim, se review rodar                  | Chave Google AI — validada pelo script de integração     |
| `OLLAMA_HOST`       | migration                 | Não                                   | Padrão: `http://localhost:11434`                         |
| `OLLAMA_MODEL`      | migration                 | Não                                   | Modelo Ollama preferido (ex.: `llama3.1`)                |


 Se Ollama estiver rodando, o `migration_agent` usa LLM local e `GROQ_API_KEY` só é necessária para review/test.

### Como obter cada chave

#### Groq (`GROQ_API_KEY`, `API_3`, `PROVIDER_API_KEY`)

1. Acesse [console.groq.com](https://console.groq.com/)
2. Crie uma conta gratuita
3. Vá em **API Keys** → **Create API Key**
4. Copie a chave (prefixo `gsk`_)
5. Use a **mesma chave** em `GROQ_API_KEY`, `API_3` e `PROVIDER_API_KEY`
6. Para o test agent, defina `PROVIDER_BASE_URL=https://api.groq.com/openai/v1`

Modelos usados via Groq neste projeto:


| Componente           | Modelo                                      |
| -------------------- | ------------------------------------------- |
| migration (fallback) | `llama-3.3-70b-versatile`                   |
| test agent           | `meta-llama/llama-4-scout-17b-16e-instruct` |
| review agent         | `llama-3.3-70b-versatile` (todos os nós)    |


#### Google AI (`GOOGLE_API_KEY`)

1. Acesse [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Clique em **Create API Key**
3. Copie a chave (prefixo `AIza`)

Necessária hoje porque `test_pipeline.py` valida sua presença antes de executar o review. Documentação futura do `review_agent` prevê Gemini nos nós pesados — quando isso for implementado, a chave passará a ser usada de fato.

#### Outros provedores OpenAI-compatíveis (test agent)

O `test_agent` usa `ChatOpenAI` com `PROVIDER_BASE_URL` + `PROVIDER_API_KEY`. Alternativas:


| Provedor    | `PROVIDER_BASE_URL`              | Como obter chave                                 |
| ----------- | -------------------------------- | ------------------------------------------------ |
| Groq        | `https://api.groq.com/openai/v1` | [console.groq.com](https://console.groq.com/)    |
| OpenRouter  | `https://openrouter.ai/api/v1`   | [openrouter.ai/keys](https://openrouter.ai/keys) |
| Together AI | `https://api.together.xyz/v1`    | [api.together.xyz](https://api.together.xyz/)    |


Ajuste o modelo em `test_agent/agent/agent.py` (`model=...`) se trocar de provedor.

### Limites das contas gratuitas (Groq)


| Modelo                    | Tokens/min (TPM) | Tokens/dia (TPD) |
| ------------------------- | ---------------- | ---------------- |
| `llama-3.1-8b-instant`    | 6.000            | 500.000          |
| `llama-3.3-70b-versatile` | 6.000            | 100.000          |


Reset diário às **00:00 UTC** (21:00 horário de Brasília).

### Alternativa: variáveis na sessão (sem `.env`)

**Linux / macOS:**

```bash
export GROQ_API_KEY="gsk_..."
export API_3="gsk_..."
export PROVIDER_API_KEY="gsk_..."
export PROVIDER_BASE_URL="https://api.groq.com/openai/v1"
export GOOGLE_API_KEY="AIza..."
```

**Windows (PowerShell):**

```powershell
$env:GROQ_API_KEY       = "gsk_..."
$env:API_3              = "gsk_..."
$env:PROVIDER_API_KEY   = "gsk_..."
$env:PROVIDER_BASE_URL  = "https://api.groq.com/openai/v1"
$env:GOOGLE_API_KEY     = "AIza..."
```

---

## 6. Ollama (opcional, recomendado)

O Ollama reduz custo e rate limits no **migration_agent**. O **test_agent** usa `PROVIDER_`* independentemente; o **review_agent** na `main` usa Groq via `API_3`.

### Linux

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1
ollama serve &
```

### macOS

```bash
brew install ollama
ollama pull llama3.1
ollama serve &
```

### Windows

1. Baixe o instalador em [ollama.com/download](https://ollama.com/download)
2. Instale e abra o Ollama
3. No terminal:

```powershell
ollama pull llama3.1
```

### Verificar

```bash
curl http://localhost:11434/api/tags
```

Se Ollama estiver ativo, `test_pipeline.py` detecta automaticamente e usa LLM local no migration (sem consumir cota Groq nessa etapa).

---

## 7. Executar o pipeline completo

Retorne à raiz do repositório com o venv ativo.

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

`PYTHONUTF8` evita `UnicodeEncodeError` com emojis nos logs do Windows.

### Opções úteis

```bash
# Mais rápido: pula test_agent
python test_pipeline.py --skip-test

# Sem review (útil se cota Groq/Google esgotada)
python test_pipeline.py --skip-review

# Entrada customizada
python test_pipeline.py --input meu_codigo_urllib.py

# Menos exemplos few-shot (economiza tokens)
python test_pipeline.py --examples 5 --skip-test

# Forçar modelo Ollama
python test_pipeline.py --ollama-model llama3.1
```

### Saída esperada (resumida)

```
  Detectando backend LLM disponível...
  [Ollama] Nao disponivel — usando Groq para todos os agentes

############################################################
  PIPELINE DE INTEGRACAO
  Input  : .../url.py
  Output : .../.pipeline_output
############################################################

============================================================
  ETAPA 1 — MIGRATION AGENT
============================================================
  Status  : validado
  ...

============================================================
  ETAPA 2 — TEST AGENT
============================================================
  Decision: APPROVED | NEEDS_REVISION
  ...

============================================================
  ETAPA 3 — REVIEW AGENT
============================================================
  Agentes acionados : ['semantica', 'seguranca', 'lint']
  Deve reprocessar  : False
  ...

############################################################
  PIPELINE CONCLUIDO
  Artefatos   : .../.pipeline_output
############################################################
```

---

## 8. Artefatos de saída

Com `test_pipeline.py`, os artefatos ficam em `.pipeline_output/`:


| Arquivo                 | Conteúdo                                  |
| ----------------------- | ----------------------------------------- |
| `migrated_code.py`      | Código migrado (urllib → requests)        |
| `migration_result.json` | Status, tempo e mensagens do migration    |
| `test_report.md`        | Relatório de equivalência (se test rodou) |
| `test_result.json`      | Métricas de cobertura e decisão do router |
| `review_report.md`      | Relatório consolidado de revisão          |
| `review_result.json`    | Achados brutos por categoria              |
| `pipeline_summary.json` | Sumário de todas as etapas                |


### Visualizar relatório

**Linux / macOS:**

```bash
cat .pipeline_output/review_report.md
```

**Windows:**

```powershell
Get-Content .pipeline_output\review_report.md
```

---

## 9. Executar agentes individualmente

### Review agent (standalone)

**Linux / macOS:**

```bash
source .venv/bin/activate
export API_3="$GROQ_API_KEY"
python review_agent/testReviewAgent.py
```

**Windows:**

```powershell
$env:API_3 = $env:GROQ_API_KEY
python review_agent\testReviewAgent.py
```

Usa os arquivos em `review_agent/test1/` como entrada.

### Review agent (API FastAPI)

```bash
cd review_agent
uvicorn review-agent:app --host 127.0.0.1 --port 8000 --reload
```

Swagger: `http://127.0.0.1:8000/docs`

### Test agent (standalone)

```bash
python test_agent/agent/agent.py \
  --input-json .pipeline_output/migration_result.json \
  --output .pipeline_output/test_report.md
```

Requer `PROVIDER_API_KEY` e `PROVIDER_BASE_URL` no `.env`.

### Migration agent (standalone)

```bash
python migration_agent/langgraph-mig03.py
```

Usa `url.py` como entrada padrão e grava `inferencia.json` na raiz.

---

## 10. Solução de problemas

### `ModuleNotFoundError`

```bash
pip install -r requirements.txt
pip install -r review_agent/requirements.txt
```

### `UnicodeEncodeError` no Windows

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

### `ValueError: API_3 não encontrada`

Defina `API_3` no `.env` com a mesma chave Groq de `GROQ_API_KEY`:

```env
API_3=gsk_...
```

### `RuntimeError: Chaves de API ausentes` (GOOGLE_API_KEY / GROQ_API_KEY)

- Defina todas as variáveis da [seção 5](#5-configurar-chaves-de-api-e-modelos), ou
- Execute com `--skip-review` se só quiser migration + test

### `groq.RateLimitError: 429`

- Aguarde 2–3 minutos e tente novamente
- Use `--skip-review` ou `--skip-test` para reduzir chamadas
- Instale Ollama para aliviar o migration
- Limite diário (TPD): aguarde reset às 00:00 UTC

### Test agent: relatório vazio ou pytest falha

- Confirme `pytest`, `pytest-cov` e `responses` instalados
- Verifique se o código migrado é Python válido (sem marcadores de merge `<<<<<<<`)
- Confirme `PROVIDER_API_KEY` e `PROVIDER_BASE_URL`

### `FileNotFoundError: Input file not found`

```bash
python test_pipeline.py --input /caminho/para/meu_codigo.py
```

### Ruff não encontrado pelo review

O `no_lint` chama `ruff` via subprocess — precisa estar no PATH:

```bash
pip install ruff
# Linux/macOS: export PATH="$HOME/.local/bin:$PATH"
```

### Ollama: connection refused

```bash
ollama serve
ollama pull llama3.1
curl http://localhost:11434/api/tags
```

---

## 11. Estrutura do repositório

```
agente-migracao-TALP/
│
├── migration_agent/
│   └── langgraph-mig03.py          # Agente de migração urllib → requests
│
├── test_agent/
│   ├── agent/agent.py              # Agente de equivalência funcional
│   ├── prompts/                    # Prompts por nó do grafo
│   └── requirements.txt
│
├── review_agent/
│   ├── prompts/                    # Templates JSON dos nós LangGraph
│   ├── test1/                      # Exemplos para testReviewAgent.py
│   ├── review-agent.py             # Orquestrador + API FastAPI
│   ├── testReviewAgent.py
│   └── requirements.txt
│
├── dataset/
│   └── Request-Urllib.xlsx         # Exemplos few-shot (GitHub)
│
├── scripts/
│   ├── run_pipeline_real.py        # Pipeline via módulo agents/ (futuro)
│   └── run_pipeline_with_feedback.py
│
├── url.py                          # Entrada urllib padrão
├── url-migrate.py                  # Referência manual (opcional)
├── inferencia.json                 # Inferência semântica (gerado)
├── test_pipeline.py                # Pipeline integrado (recomendado)
├── requirements.txt                # Dependências da raiz
├── .env                            # Chaves (não commitado)
├── .pipeline_output/               # Saída do test_pipeline.py
├── PIPELINE.md                     # Documentação do test_pipeline.py
├── REPLICACAO.md                   # Este guia
└── README.md
```

---

## Referências

- [LangGraph — Documentação oficial](https://langchain-ai.github.io/langgraph/)
- [Groq — Console e API Keys](https://console.groq.com/)
- [Groq — OpenAI-compatible API](https://console.groq.com/docs/openai)
- [Google AI Studio — API Keys](https://aistudio.google.com/apikey)
- [Ollama — Download e modelos](https://ollama.com/)
- [Ruff — Linter Python](https://docs.astral.sh/ruff/)

