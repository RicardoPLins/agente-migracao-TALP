# Pipeline de migração: migration_agent → test_agent → review_agent
#
# Build:
#   docker build -t migracao-talp .
#
# Run (veja DOCKER.md para detalhes e configuração do Ollama):
#   docker run --rm --env-file .env -v $(pwd)/.pipeline_output:/app/.pipeline_output migracao-talp

FROM python:3.12-slim

# git    -> usado pelo review_agent (git diff --no-index para gerar o diff estruturado)
# ruff   -> instalado via pip (review_agent/requirements.txt), mas precisa de gcc/build
#           para algumas dependências sem wheel pronto em todas as arquiteturas
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependências primeiro (cache de camada em rebuilds)
COPY requirements.txt ./requirements.txt
COPY review_agent/requirements.txt review_agent/requirements.txt
COPY test_agent/requirements.txt test_agent/requirements.txt
RUN pip install --no-cache-dir \
    -r requirements.txt \
    -r review_agent/requirements.txt \
    -r test_agent/requirements.txt

# Código do projeto
COPY . .

ENTRYPOINT ["python", "test_pipeline.py"]
