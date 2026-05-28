# Teste standalone do review agent (`testReviewAgent.py`)

Guia passo a passo para rodar **pela primeira vez** o script de teste isolado do agente de code review, sem subir o pipeline completo (`test_pipeline.py`).

O script compara dois arquivos de exemplo em `review_agent/test1/` (`original.py` e `migrado.py`), executa o grafo LangGraph do review e imprime o relatório Markdown no terminal.

---

## O que você precisa antes de começar

| Requisito | Versão / detalhe | Para quê |
|-----------|------------------|----------|
| **Python** | 3.11 ou superior | Executar o script e instalar dependências |
| **Conta Groq** | [console.groq.com](https://console.groq.com/) | Chamadas LLM (parser, semântica, segurança, lint, crítico, relatório) |
| **Chave `API_3`** | API key Groq no `.env` | Autenticação em `_get_llm()` |
| **Git** | No `PATH` | `git diff --no-index` no nó `no_parser` (recomendado) |
| **Ruff** | Instalado via pip | Nó `no_lint` (análise estática) |

> **Tempo esperado:** entre ~1 e 5 minutos por execução, dependendo da cota e latência da Groq. O grafo faz várias chamadas ao modelo `llama-3.3-70b-versatile`.

---

## Passo 1 — Abrir o repositório

```powershell
cd \agente-migracao-TALP
```

No Linux/macOS, use o caminho equivalente onde o repositório foi clonado.

---

## Passo 2 — Criar e ativar o ambiente virtual

Recomenda-se um venv na **raiz do repositório**:

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux / macOS:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Se o PowerShell bloquear a ativação, execute uma vez (como administrador):

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

---

## Passo 3 — Instalar dependências

Na raiz do repo, com o venv ativo:

```powershell
pip install -r requirements.txt
pip install -r review_agent\requirements.txt
```

Isso instala LangGraph, LangChain, Groq, FastAPI, `python-dotenv`, **Ruff**, etc.

Confirme que o Python enxerga o Ruff:

```powershell
ruff --version
git --version
```

Se `git` ou `ruff` não forem encontrados, corrija o `PATH` ou reinstale antes de continuar.

---

## Passo 4 — Configurar a chave Groq (`.env`)

1. Copie o exemplo (se ainda não tiver um `.env`):

   ```powershell
   copy .env.example .env
   ```

2. Edite `.env` na **raiz do repositório** e preencha pelo menos:

   ```env
   API_3=gsk_sua_chave_aqui
   ```

   Obtenha a chave em: [console.groq.com/keys](https://console.groq.com/keys)

3. O script `testReviewAgent.py` carrega automaticamente:
   - `review_agent/.env` (se existir)
   - `.env` na raiz do repo (prioridade menor — não sobrescreve variáveis já definidas)

> **Importante:** o review agent usa **somente** `API_3`. Variáveis como `GROQ_API_KEY` ou `PROVIDER_API_KEY` são do pipeline integrado; não são necessárias para este teste standalone.

---

## Passo 5 — (Opcional) Conferir os arquivos de entrada

O teste usa exemplos fixos:

| Arquivo | Caminho |
|---------|---------|
| Código original (urllib) | `review_agent/test1/original.py` |
| Código migrado (requests) | `review_agent/test1/migrado.py` |

Para testar outro par de arquivos, edite `testReviewAgent.py` (linhas que leem `test1/original.py` e `test1/migrado.py`) ou substitua temporariamente o conteúdo desses arquivos.

---

## Passo 6 — Executar o teste

Com o venv ativo e `.env` configurado, na **raiz do repositório**:

**Windows (PowerShell):**

```powershell
python review_agent\testReviewAgent.py
```

**Linux / macOS:**

```bash
python review_agent/testReviewAgent.py
```

---

## Passo 7 — Interpretar a saída

Se tudo correu bem, o terminal exibe um relatório Markdown completo, por exemplo:

```markdown
# Relatório de Revisão — Migração de Código

## Legenda de severidade
...

## 1. Resumo executivo
...

## 2. Veredito
...
```

Seções principais:

- **Veredito** — APROVADO, APROVADO COM RESSALVAS ou indicador de reprovação
- **Achados P0–P3** — problemas por severidade
- **Detalhamento por agente** — semântica, segurança, lint
- **Notas sobre localização de linhas** — correção automática de linhas citadas pelo modelo

Não é gerado arquivo de saída por padrão; o relatório vai só para o stdout. Para salvar:

```powershell
python review_agent\testReviewAgent.py > review_agent\test1\saida.txt
```

---

## Fluxo interno (resumo)

```
testReviewAgent.py
  │
  ├─ Carrega .env (API_3)
  ├─ Lê test1/original.py + test1/migrado.py
  └─ Chama review-agent._executar_grafo()
         │
         ├─ no_parser        (git diff + LLM)
         ├─ no_classificador (LLM)
         ├─ no_semantico / no_seguranca / no_lint (paralelo)
         ├─ no_critico       (reflection, até 3 iterações)
         └─ relatorio_final  (Markdown)
```

---

## Problemas comuns

### `ValueError: API_3 não encontrada nas variáveis de ambiente`

- Crie ou corrija o `.env` na raiz (ou em `review_agent/.env`)
- Confirme que a linha não tem aspas quebradas: `API_3=gsk_...`
- Rode o script a partir da raiz do repo (o script procura `.env` em dois locais)

### `groq.RateLimitError` (429) — limite de tokens

A Groq free tier tem limites diários (TPD) e por minuto (TPM) no modelo `llama-3.3-70b-versatile`.

- Aguarde o tempo indicado na mensagem de erro
- Consulte uso em [console.groq.com/settings/limits](https://console.groq.com/settings/limits)
- Evite rodar o pipeline completo e o teste standalone em sequência no mesmo dia se a cota estiver baixa

### `ModuleNotFoundError` (langgraph, langchain_groq, dotenv, …)

- Ative o venv: `.\.venv\Scripts\Activate.ps1`
- Reinstale: `pip install -r review_agent\requirements.txt`

### Ruff ou git não encontrado

- **Ruff:** `pip install ruff` (já incluso em `review_agent/requirements.txt`)
- **Git:** instale [Git for Windows](https://git-scm.com/download/win) e reinicie o terminal

Sem git, o parser usa fallback via LLM (menos determinístico, mais tokens).

### Execução muito lenta ou “travada”

Normal: várias chamadas LLM em sequência/paralelo. Aguarde alguns minutos. Se houver 429, o processo pode pausar até a janela de rate limit liberar.

---

## Próximos passos

- Ler a arquitetura completa: [README.md](./README.md)
- Rodar o pipeline integrado (migration + test + review): [REPLICACAO.md](../REPLICACAO.md)
- Subir a API HTTP do review agent: `uvicorn review-agent:app --host 127.0.0.1 --port 8000` (dentro de `review_agent/`)

---

## Checklist rápido (primeira execução)

- [ ] Python 3.11+ instalado
- [ ] Repositório clonado / aberto
- [ ] `.venv` criado e ativado
- [ ] `pip install -r requirements.txt` e `pip install -r review_agent/requirements.txt`
- [ ] `git --version` e `ruff --version` OK
- [ ] `.env` com `API_3=gsk_...`
- [ ] `python review_agent\testReviewAgent.py` executado com sucesso
