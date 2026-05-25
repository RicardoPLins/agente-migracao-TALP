# Guia de Replicação — Pipeline de Migração Automatizada

Este guia detalha **exatamente** os passos necessários para replicar o experimento em uma máquina nova, do zero à execução completa do pipeline `migration_agent → test_agent → review_agent`.

---

## Sumário

1. [Pré-requisitos de sistema](#1-pré-requisitos-de-sistema)
2. [Clonar o repositório](#2-clonar-o-repositório)
3. [Criar e configurar o ambiente virtual](#3-criar-e-configurar-o-ambiente-virtual)
4. [Instalar as dependências](#4-instalar-as-dependências)
5. [Configurar as chaves de API](#5-configurar-as-chaves-de-api)
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
| Python | 3.10+ | `python --version` |
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
# Dependências principais do review_agent (inclui langchain-google-genai)
pip install -r requirements.txt

# Dependências adicionais dos outros agentes (instaladas no mesmo venv)
pip install openpyxl langchain-ollama langchain-openai pytest pytest-cov responses pytest-responses
```

> **Por que langchain-ollama se não usamos Ollama necessariamente?**
> O `migration_agent` e o `test_agent` importam `langchain_ollama` no nível de módulo. O `test_pipeline.py` detecta Ollama automaticamente e usa o pacote nativo se disponível, ou substitui `ChatOllama` por `ChatGroq` via monkey-patch se não estiver rodando. O pacote precisa estar instalado de qualquer forma.

### Verificar instalação

```bash
python -c "import langgraph, langchain_groq, langchain_google_genai, langchain_ollama, openpyxl, fastapi; print('OK')"
```

Saída esperada: `OK`

---

## 5. Configurar as chaves de API

O pipeline usa **dois provedores de LLM**:

| Provedor | Usado por | Onde obter |
|---|---|---|
| **Groq** | migration_agent, test_agent (fallback), nós leves do review | [console.groq.com](https://console.groq.com/) |
| **Google AI (Gemini)** | nós pesados do review_agent | [aistudio.google.com](https://aistudio.google.com/apikey) |

### Obter a chave Groq

1. Acesse [console.groq.com](https://console.groq.com/)
2. Crie uma conta gratuita
3. Em **API Keys**, clique em **Create API Key**
4. Copie a chave (começa com `gsk_`)

### Obter a chave Google AI

1. Acesse [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Clique em **Create API Key**
3. Copie a chave (começa com `AIza`)

### Configurar

**Opção A — arquivo `.env`** (recomendado para uso recorrente):

Crie `agente-migracao-TALP/.env`:

```env
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
GOOGLE_API_KEY=AIzaxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Opção B — variáveis de ambiente na sessão** (execução única):

```powershell
# Windows PowerShell
$env:GROQ_API_KEY   = "gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
$env:GOOGLE_API_KEY = "AIzaxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

```bash
# Linux / macOS
export GROQ_API_KEY="gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export GOOGLE_API_KEY="AIzaxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### Limites das contas gratuitas

| Provedor / Modelo | Tokens/min (TPM) | Tokens/dia (TPD) |
|---|---|---|
| Groq — `llama-3.1-8b-instant` | 6.000 | 500.000 |
| Groq — `llama-3.3-70b-versatile` | 6.000 | 100.000 |
| Google Gemini 2.5 Flash | 1.000.000 | sem limite gratuito |

> Com Gemini nos nós pesados do review, os erros 429 são praticamente eliminados. A cota Groq só afeta os nós leves (classificador, lint, relatório) e os agentes de migration/test quando Ollama não está disponível.

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
# Se não criou .env:
$env:GROQ_API_KEY   = "gsk_..."
$env:GOOGLE_API_KEY = "AIza..."

.\review_agent\.venv\Scripts\python.exe test_pipeline.py
```

### Linux / macOS

```bash
PYTHONUTF8=1 ./review_agent/.venv/bin/python test_pipeline.py
```

> As variáveis `PYTHONUTF8` e `PYTHONIOENCODING` são necessárias no Windows para que os emojis nos prints dos agentes não causem `UnicodeEncodeError` no terminal.

### Modos de execução recomendados

```bash
# Execução mais rápida: pula o test_agent (~50s total)
python test_pipeline.py --skip-test

# Execução econômica em tokens: só migration + test
python test_pipeline.py --skip-review

# Usar código próprio como entrada
python test_pipeline.py --input meu_codigo_urllib.py

# Reduzir exemplos few-shot para economizar tokens (padrão: 10)
python test_pipeline.py --examples 5 --skip-test

# Forçar modelo Ollama específico (se Ollama disponível)
python test_pipeline.py --ollama-model llama3.1
```

### Saída esperada no terminal

```
  Detectando backend LLM disponível...
  [Ollama] Nao disponivel — usando Groq para todos os agentes

############################################################
  PIPELINE DE INTEGRAÇÃO
  Início: 2026-05-24 21:00:00
  Input : .../url.py
  Output: .../.pipeline_output
  Ollama : NAO (usando Groq)
############################################################

============================================================
  ETAPA 1 — MIGRATION AGENT
  Backend : Groq (llama-3.1-8b-instant)
============================================================
  Carregando 10 exemplos do dataset...
  Status  : validado
  Tempo   : ~24s
  Linhas  : 222

  ETAPA 2 — TEST AGENT: pulada (--skip-test)

  Aguardando 10s antes do review (Groq rate limit)...

============================================================
  ETAPA 3 — REVIEW AGENT
  Backend : Gemini 2.5 Flash (nos pesados) + Groq 8B (nos leves)
============================================================
  Executando grafo de revisão (tentativa 1/3)...
  Agentes acionados : ['semantica', 'seguranca', 'lint']
  Iteracoes critico : 1
  Deve reprocessar  : False
  Achados semantica : 1
  Achados seguranca : 0
  Achados lint      : 0
  Tempo             : ~46s

############################################################
  PIPELINE CONCLUÍDO
  Tempo total : ~80s
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

### Via script de teste direto

```powershell
# Certifique-se que as chaves estão no .env ou na sessão
.\review_agent\.venv\Scripts\python.exe .\review_agent\testReviewAgent.py
```

O arquivo `testReviewAgent.py` usa os arquivos em `review_agent/test1/` como entrada.

### Via API FastAPI

```bash
cd review_agent
.\.venv\Scripts\python.exe -m uvicorn "review-agent:app" --host 127.0.0.1 --port 8000 --reload
```

Acesse `http://127.0.0.1:8000/docs` para a interface Swagger.

### Via PowerShell (Invoke-RestMethod)

```powershell
$original = Get-Item ".\url.py"
$migrado  = Get-Item ".\review_agent\test1\migrado.py"

Invoke-RestMethod -Uri "http://localhost:8000/review/files" -Method Post -Form @{
    original = $original
    migrado  = $migrado
}
```

---

## 10. Solução de problemas comuns

### `ModuleNotFoundError: No module named 'langchain_ollama'`

```bash
pip install langchain-ollama
```

### `ModuleNotFoundError: No module named 'langchain_google_genai'`

```bash
pip install langchain-google-genai
```

### `ModuleNotFoundError: No module named 'openpyxl'`

```bash
python -m pip install openpyxl
```

### `UnicodeEncodeError: 'charmap' codec can't encode character` (Windows)

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

### `ValueError: GOOGLE_API_KEY não encontrada`

O review_agent exige `GOOGLE_API_KEY` para os nós pesados (parser, semântico, segurança, crítico). Defina no `.env` ou na sessão:

```powershell
$env:GOOGLE_API_KEY = "AIza..."
```

### `groq.RateLimitError: 429` (por minuto)

O `review-agent.py` já tem retry interno com backoff (30s → 60s → 120s). O `test_pipeline.py` tem retry externo adicional (45s → 90s). Se persistir, aguarde 2–3 minutos e tente novamente.

Com Gemini nos nós pesados, esse erro só ocorre nos nós leves (classificador, lint, relatório — consumo muito menor).

### `groq.RateLimitError: 429` (diário — TPD)

```
Rate limit reached ... on tokens per day (TPD): Limit 100000, Used 97385
```

Aguarde o reset às **00:00 UTC** (21:00 horário de Brasília). Use `--skip-review` enquanto aguarda.

### `google.api_core.exceptions.ResourceExhausted` (Gemini)

Limite de TPM do Gemini atingido — improvável no free tier (1M TPM), mas possível em uso intenso. O `_invoke_com_retry` já trata automaticamente.

### `Error code: 413` no test_agent (prompt muito grande)

O `node_analyzer` do test_agent envia ambos os códigos completos para o modelo. Com o modelo 8B (TPM 6.000), o prompt pode exceder o limite. O pipeline já usa automaticamente o 70B para o test_agent.

### `FileNotFoundError: Input file not found`

```bash
python test_pipeline.py --input /caminho/para/meu_codigo.py
```

### `Dataset not found` no migration_agent

O arquivo `dataset/Request-Urllib.xlsx` precisa existir. Se não estiver presente, o agente continua sem exemplos few-shot. Verifique se o clone incluiu a pasta `dataset/`.

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
│   └── requirements.txt
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
│   ├── testReviewAgent.py       # Script de teste standalone do review_agent
│   ├── requirements.txt
│   └── README.md
│
├── dataset/
│   └── Request-Urllib.xlsx      # Exemplos reais de migração (GitHub)
│
├── url.py                       # Código urllib de entrada padrão (ConversationScraper)
├── url-migrate.py               # Código migrado manualmente (referência)
├── test_pipeline.py             # Script de integração dos três agentes
├── .env                         # Chaves de API (não commitado — ver .gitignore)
├── .gitignore
│
├── PIPELINE.md                  # Documentação do test_pipeline.py
├── REPLICACAO.md                # Este guia
└── README.md                    # README geral do projeto
```

---

## Referências

- [LangGraph — Documentação oficial](https://langchain-ai.github.io/langgraph/)
- [Groq — Console e API Keys](https://console.groq.com/)
- [Google AI Studio — API Keys](https://aistudio.google.com/apikey)
- [Ruff — Linter Python](https://docs.astral.sh/ruff/)
- [awesome-reviewers — Skills de revisão](https://github.com/baz-scm/awesome-reviewers)
- [pr-agent (Qodo) — Referência de prompts](https://github.com/Codium-ai/pr-agent)
