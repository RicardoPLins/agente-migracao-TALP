from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agents.migration import run_migration
from agents.review import run_migration_review
from agents.equivalence_subprocess import run_equivalence_from_review_output


class MigrateRequest(BaseModel):
    code: str = Field(..., description="Python code using urllib")
    num_examples: int = Field(30, ge=1, le=200)


class ReviewRequest(BaseModel):
    original_code: str
    migrated_code: str
    semantic_inference: Optional[Any] = None


class TestRequest(BaseModel):
    review_output: dict[str, Any]
    timeout_s: int = Field(300, ge=30, le=900)


class PipelineRequest(BaseModel):
    code: str
    num_examples: int = Field(30, ge=1, le=200)
    run_tests: bool = True
    test_timeout_s: int = Field(300, ge=30, le=900)


def _index_html() -> str:
    return """<!doctype html>
<html lang=\"pt-BR\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Agent Migration UI</title>
    <style>
        :root {
            color-scheme: dark;
            --bg: #0b1020;
            --panel: #121a31;
            --panel-2: #0f1730;
            --border: rgba(255,255,255,.10);
            --text: #e5e7eb;
            --muted: #9ca3af;
            --accent: #7c3aed;
            --accent-2: #22c55e;
            --danger: #ef4444;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif;
            background: radial-gradient(circle at top, #18233f 0%, var(--bg) 50%);
            color: var(--text);
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 32px 20px 48px;
        }
        .hero {
            display: grid;
            gap: 8px;
            margin-bottom: 20px;
        }
        .title {
            font-size: clamp(2rem, 4vw, 3rem);
            font-weight: 800;
            letter-spacing: -0.03em;
            margin: 0;
        }
        .subtitle {
            color: var(--muted);
            margin: 0;
            max-width: 72ch;
            line-height: 1.5;
        }
        .grid {
            display: grid;
            grid-template-columns: minmax(0, 1.1fr) minmax(360px, .9fr);
            gap: 18px;
        }
        .card {
            background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02));
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 30px 60px rgba(0,0,0,.25);
            backdrop-filter: blur(12px);
        }
        .card h2 {
            font-size: 1rem;
            margin: 0 0 12px;
        }
        label {
            display: block;
            font-size: .9rem;
            color: var(--muted);
            margin-bottom: 8px;
        }
        textarea, input[type=\"number\"], input[type=\"text\"] {
            width: 100%;
            border: 1px solid var(--border);
            border-radius: 14px;
            background: var(--panel-2);
            color: var(--text);
            padding: 14px;
            font: inherit;
            outline: none;
        }
        textarea {
            min-height: 520px;
            resize: vertical;
            line-height: 1.5;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        }
        .row {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            margin-top: 14px;
        }
        .checks {
            display: flex;
            gap: 18px;
            align-items: center;
            margin-top: 14px;
            flex-wrap: wrap;
        }
        .checks label {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin: 0;
            color: var(--text);
        }
        .actions {
            display: flex;
            gap: 12px;
            margin-top: 16px;
            flex-wrap: wrap;
        }
        button {
            border: 0;
            border-radius: 14px;
            padding: 12px 18px;
            font-weight: 700;
            cursor: pointer;
            color: white;
            background: linear-gradient(135deg, var(--accent), #4f46e5);
        }
        button.secondary {
            background: rgba(255,255,255,.08);
            color: var(--text);
            border: 1px solid var(--border);
        }
        pre {
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            background: #081022;
            color: #d1fae5;
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 14px;
            min-height: 340px;
            overflow: auto;
        }
        .status {
            margin-top: 12px;
            color: var(--muted);
            min-height: 24px;
        }
        .status.error { color: #fca5a5; }
        .status.success { color: #86efac; }
        .hint {
            font-size: .85rem;
            color: var(--muted);
            margin-top: 10px;
            line-height: 1.4;
        }
        @media (max-width: 980px) {
            .grid { grid-template-columns: 1fr; }
            textarea { min-height: 360px; }
        }
    </style>
</head>
<body>
    <main class=\"container\">
        <section class=\"hero\">
            <h1 class=\"title\">Agent Migration UI</h1>
            <p class=\"subtitle\">Cole o código-fonte no painel da esquerda, ajuste os parâmetros e execute a migração completa via <code>/pipeline</code>.</p>
        </section>

        <section class=\"grid\">
            <div class=\"card\">
                <h2>Código de entrada</h2>
                <label for=\"code\">Código Python para migrar</label>
                <textarea id=\"code\" spellcheck=\"false\" placeholder=\"Cole aqui o código que usa urllib, requests ou outro fluxo que você quer migrar.\"></textarea>

                <div class=\"row\">
                    <div>
                        <label for=\"num_examples\">Exemplos</label>
                        <input id=\"num_examples\" type=\"number\" min=\"1\" max=\"200\" value=\"30\" />
                    </div>
                    <div>
                        <label for=\"timeout\">Timeout dos testes (s)</label>
                        <input id=\"timeout\" type=\"number\" min=\"30\" max=\"900\" value=\"300\" />
                    </div>
                    <div>
                        <label for=\"endpoint\">Endpoint</label>
                        <input id=\"endpoint\" type=\"text\" value=\"/pipeline\" />
                    </div>
                </div>

                <div class=\"checks\">
                    <label><input id=\"run_tests\" type=\"checkbox\" checked /> Executar testes</label>
                    <label><input id=\"pretty_json\" type=\"checkbox\" checked /> Formatar JSON</label>
                </div>

                <div class=\"actions\">
                    <button id=\"run\" type=\"button\">Executar migração</button>
                    <button id=\"clear\" class=\"secondary\" type=\"button\">Limpar saída</button>
                </div>

                <p class=\"hint\">Dica: este painel envia o conteúdo para o backend Python sem precisar de outra interface separada.</p>
            </div>

            <div class=\"card\">
                <h2>Resultado</h2>
                <pre id=\"output\">Aguardando entrada…</pre>
                <div id=\"status\" class=\"status\"></div>
            </div>
        </section>
    </main>

    <script>
        const codeEl = document.getElementById('code');
        const numExamplesEl = document.getElementById('num_examples');
        const timeoutEl = document.getElementById('timeout');
        const endpointEl = document.getElementById('endpoint');
        const runTestsEl = document.getElementById('run_tests');
        const prettyJsonEl = document.getElementById('pretty_json');
        const outputEl = document.getElementById('output');
        const statusEl = document.getElementById('status');
        const runBtn = document.getElementById('run');
        const clearBtn = document.getElementById('clear');

        codeEl.value = `import urllib.request\n\n\nurl = \"https://example.com\"\nwith urllib.request.urlopen(url) as response:\n    print(response.read().decode())\n`;

        function setStatus(message, kind = '') {
            statusEl.className = kind ? `status ${kind}` : 'status';
            statusEl.textContent = message;
        }

        function renderOutput(value) {
            if (prettyJsonEl.checked) {
                outputEl.textContent = JSON.stringify(value, null, 2);
            } else {
                outputEl.textContent = JSON.stringify(value);
            }
        }

        runBtn.addEventListener('click', async () => {
            const code = codeEl.value.trim();
            if (!code) {
                setStatus('Cole um código antes de executar.', 'error');
                return;
            }

            runBtn.disabled = true;
            setStatus('Executando pipeline...', '');
            outputEl.textContent = 'Processando...';

            const payload = {
                code,
                num_examples: Number(numExamplesEl.value || 30),
                run_tests: runTestsEl.checked,
                test_timeout_s: Number(timeoutEl.value || 300),
            };

            try {
                const response = await fetch(endpointEl.value || '/pipeline', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });

                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data?.detail ? JSON.stringify(data.detail) : `HTTP ${response.status}`);
                }

                renderOutput(data);
                setStatus('Pipeline concluído com sucesso.', 'success');
            } catch (error) {
                outputEl.textContent = String(error?.stack || error);
                setStatus('Falha ao executar o pipeline.', 'error');
            } finally {
                runBtn.disabled = false;
            }
        });

        clearBtn.addEventListener('click', () => {
            outputEl.textContent = 'Aguardando entrada…';
            setStatus('');
        });
    </script>
</body>
</html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Gateway", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_index_html())

    @app.post("/migrate")
    def migrate(req: MigrateRequest) -> dict[str, Any]:
        return run_migration(req.code, num_examples=req.num_examples)

    @app.post("/review")
    def review(req: ReviewRequest) -> dict[str, Any]:
        return run_migration_review(
            original_code=req.original_code,
            migrated_code=req.migrated_code,
            semantic_inference=req.semantic_inference,
        )

    @app.post("/test")
    def test(req: TestRequest) -> dict[str, Any]:
        return run_equivalence_from_review_output(req.review_output, timeout_s=req.timeout_s)

    @app.post("/pipeline")
    def pipeline(req: PipelineRequest) -> dict[str, Any]:
        migration_output = run_migration(req.code, num_examples=req.num_examples)
        review_output = run_migration_review(
            original_code=migration_output["original_code"],
            migrated_code=migration_output.get("migrated_code", ""),
            semantic_inference=migration_output.get("semantic_inference"),
        )

        test_result: Optional[dict[str, Any]] = None
        if req.run_tests:
            test_result = run_equivalence_from_review_output(review_output, timeout_s=req.test_timeout_s)

        return {
            "migration_agent": migration_output,
            "review_agent": review_output,
            "test_agent": test_result,
        }

    return app


app = create_app()
