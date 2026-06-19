# Rodando o pipeline com Docker

Este guia mostra como rodar `test_pipeline.py` (migration_agent → test_agent →
review_agent) dentro de um container, sem precisar instalar Python, dependências
ou configurar `ruff`/`git` manualmente.

---

## 1. Pré-requisitos

- [Docker](https://docs.docker.com/get-docker/) instalado e em execução
- Docker Compose v2 (já incluso no Docker Desktop e no Docker Engine recente)
- Pelo menos uma chave de API Groq (ou Ollama rodando localmente — veja a seção 5)

---

## 2. Configurar as variáveis de ambiente

Copie o template e preencha suas chaves:

```bash
cp .env.example .env
```

Edite `.env` e defina no mínimo:

```env
GROQ_API_KEY="gsk_..."
API_3="gsk_..."               # usado pelo review_agent
PROVIDER_API_KEY="gsk_..."    # usado pelo test_agent
PROVIDER_BASE_URL="https://api.groq.com/openai/v1"
```

> O arquivo `.env` **não** é copiado para a imagem (está no `.dockerignore`).
> Ele é injetado em tempo de execução via `env_file` / `--env-file`.

---

## 3. Build da imagem

```bash
docker build -t migracao-talp .
```

ou, usando Compose (recomendado, já cuida do build + volumes):

```bash
docker compose build
```

---

## 4. Rodar o pipeline

### Opção A — Docker Compose (recomendado)

```bash
docker compose run --rm pipeline
```

Isso executa `python test_pipeline.py` com os parâmetros padrão:
- Entrada: `url.py` (montado como volume, somente leitura)
- Saída: `.pipeline_output/` (montado como volume — os artefatos ficam
  disponíveis no seu host após a execução)

Para passar argumentos extras (ver seção 6):

```bash
docker compose run --rm pipeline --skip-review
```

### Opção B — `docker run` direto

```bash
docker run --rm \
  --env-file .env \
  -v "$(pwd)/.pipeline_output:/app/.pipeline_output" \
  -v "$(pwd)/url.py:/app/url.py:ro" \
  migracao-talp
```

Com argumentos extras:

```bash
docker run --rm \
  --env-file .env \
  -v "$(pwd)/.pipeline_output:/app/.pipeline_output" \
  -v "$(pwd)/url.py:/app/url.py:ro" \
  migracao-talp --skip-test --examples 5
```

---

## 5. Usando Ollama local (opcional)

Se você tem o [Ollama](https://ollama.com) rodando na sua máquina (fora do
container) com um modelo já baixado (ex.: `ollama pull qwen2.5:14b`), o
pipeline detecta isso automaticamente e usa Ollama para `migration`/`test`
em vez de Groq.

Dentro do container, `localhost` aponta para o próprio container, não para o
host. Por isso o `docker-compose.yml` já define:

```yaml
environment:
  OLLAMA_HOST: http://host.docker.internal:11434
extra_hosts:
  - "host.docker.internal:host-gateway"
```

- **macOS / Windows (Docker Desktop):** `host.docker.internal` já funciona
  nativamente — nenhuma ação extra necessária.
- **Linux:** o `extra_hosts` com `host-gateway` resolve `host.docker.internal`
  para o IP do host (requer Docker 20.10+).

Com `docker run` direto, adicione manualmente:

```bash
docker run --rm \
  --env-file .env \
  --add-host=host.docker.internal:host-gateway \
  -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -v "$(pwd)/.pipeline_output:/app/.pipeline_output" \
  migracao-talp
```

Se preferir usar somente Groq, não defina `OLLAMA_HOST` (ou aponte para um
host que não responde) — o pipeline cai automaticamente para Groq.

---

## 6. Argumentos do `test_pipeline.py`

Todos os argumentos de `python test_pipeline.py --help` podem ser passados
após o nome da imagem (no `docker run`) ou no `command:` do compose:

| Argumento | Descrição | Padrão |
| --- | --- | --- |
| `--input ARQUIVO` | Arquivo Python urllib de entrada | `url.py` |
| `--output-dir DIR` | Diretório para salvar os artefatos | `.pipeline_output` |
| `--examples N` | Nº de exemplos few-shot para o migration_agent | `30` |
| `--skip-test` | Pula a etapa do test_agent | — |
| `--skip-review` | Pula a etapa do review_agent | — |
| `--ollama-model NOME` | Força um modelo Ollama específico | auto-detectado |

---

## 7. Migrando seu próprio arquivo

Monte seu arquivo no lugar de `url.py`:

```bash
docker run --rm \
  --env-file .env \
  -v "$(pwd)/.pipeline_output:/app/.pipeline_output" \
  -v "$(pwd)/meu_codigo.py:/app/meu_codigo.py:ro" \
  migracao-talp --input meu_codigo.py
```

---

## 8. Resultados

Após a execução, os artefatos (código migrado, relatórios de teste e review,
`pipeline_summary.json`) ficam em `.pipeline_output/` no seu diretório local.
