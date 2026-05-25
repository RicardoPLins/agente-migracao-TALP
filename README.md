# agente-migra-o-TALP
Um agente que faz migração de código (ex.: urllib -> requests), revisão e testes de equivalência usando agentes separados (migration, review, test).

# agente-migra-o-TALP

**Quick start**

- Abra o repositório e use o virtualenv em `.venv/` ou crie um novo.

**Prerequisites**

- Python 3.11+ and a virtual environment.
- Ollama (optional, recommended for local LLMs). Use Homebrew on macOS:

```bash
brew install ollama
ollama pull llama3
ollama serve &
```

**Setup**

```bash
cd /path/to/agente-migracao-TALP
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` at the repo root when using external providers (optional for local Ollama):

```
GROQ_API_KEY="your-groq-key"
PROVIDER_BASE_URL="https://your-provider"
PROVIDER_API_KEY="your-provider-key"
```

**Run the full pipeline**

```bash
source .venv/bin/activate
python scripts/run_pipeline_real.py --examples 3 --timeout 300
```

This runs migration → review → test and saves outputs to `.run_output/`.

**Open the web interface**

```bash
source .venv/bin/activate
uvicorn api.app:app --reload
```

Then open `http://127.0.0.1:8000/` and paste the code you want to migrate.

**Run only the test agent**

```bash
source .venv/bin/activate
python test_agent/agent/agent.py --input-json .run_output/pipeline_output.json --output .run_output/test_report.md
```

**Troubleshooting**

- Ollama connection refused: ensure `ollama serve` is running and the requested model is pulled (`ollama pull llama3`).
- Groq rate limits (429): switch to local Ollama or wait / use another Groq key.
- Pytest not running / empty test report: install `pytest` and `pytest-cov` (already in `requirements.txt`) and ensure `original_code` and `migrated_code` are valid Python (remove git merge conflict markers like `<<<<<<<`, `=======`, `>>>>>>>` and update Python2 exception syntax `except E, e:` → `except E as e:`).

**Outputs**

- `.run_output/pipeline_output.json` — full pipeline JSON
- `.run_output/test_report.md` — test report
- `inferencia.json` — semantic inference (if produced)
