# agente-migracao-TALP

Pipeline de migração automatizada de código Python (ex.: `urllib` → `requests`) composto por três agentes independentes orquestrados em sequência: **migration**, **test** e **review**.

Para replicar o projeto do zero em Linux, macOS ou Windows — incluindo instalação, chaves de API e troubleshooting — consulte **[REPLICACAO.md](REPLICACAO.md)**.

---

## Visão geral do pipeline

```
url.py (código urllib)
    │
    ▼
 migration_agent  →  test_agent  →  review_agent
    │                      │               │
    └─ código migrado ─────┴───────────────┘
```

O script `test_pipeline.py` na raiz integra os três agentes e grava os artefatos em `.pipeline_output/`.

---

## Agentes

### migration_agent

**Arquivo:** `migration_agent/langgraph-mig03.py`

Migra código legado (`urllib`) para `requests` usando LangGraph e few-shot learning a partir do dataset `dataset/Request-Urllib.xlsx`.

**Fluxo interno:**

```
receber → migrar → validar → END
```


| Nó        | Responsabilidade                                                |
| --------- | --------------------------------------------------------------- |
| `receber` | Recebe o código original do usuário                             |
| `migrar`  | LLM infere o comportamento e gera o código migrado              |
| `validar` | Checa heurísticas básicas (imports, sintaxe, uso de `requests`) |


**Saída:** código migrado

**Backend:** Ollama local (se disponível) ou Groq (`llama-3.3-70b-versatile`).

---

### test_agent

**Arquivo:** `test_agent/agent/agent.py`

Gera e executa testes de equivalência funcional entre o código original e o migrado, medindo cobertura e regressões.

**Fluxo interno:**

```
analyzer → inspector → generator → executor → evaluator → router → report
```


| Nó          | Responsabilidade                                                 |
| ----------- | ---------------------------------------------------------------- |
| `analyzer`  | Mapeia funções, endpoints e cenários de teste                    |
| `inspector` | Inspeciona detalhes de implementação (gzip, erros HTTP, imports) |
| `generator` | Gera `test_equivalence.py` com pytest                            |
| `executor`  | Roda pytest + pytest-cov em subprocess                           |
| `evaluator` | Calcula cobertura (≥ 80%) e equivalência (≥ 90%)                 |
| `router`    | Decide se a migração precisa de revisão (`NEEDS_REVISION`)       |
| `report`    | Produz relatório Markdown com veredito                           |


**Saída:** relatório de equivalência (`test_report.md`) e métricas JSON.

**Backend:** API compatível com OpenAI via `PROVIDER_API_KEY` + `PROVIDER_BASE_URL` (ex.: Groq OpenAI endpoint).

---

### review_agent

**Arquivo:** `review_agent/review-agent.py`

Revisa a migração comparando original e migrado. Não corrige o código — entrega um relatório com achados priorizados (P0–P3) para humano ou reprocessamento.

**Fluxo interno:**

```
parser → classificador → roteador ──► semantico  ─┐
                                      seguranca  ─┤
                                      lint       ─┘
                                            │
                                       critico → relatorio_final → END
                                            │
                                      (reflection loop, até 3×)
```


| Nó                | Responsabilidade                                        |
| ----------------- | ------------------------------------------------------- |
| `parser`          | `git diff --no-index` + extração estrutural do diff     |
| `classificador`   | Decide quais especialistas acionar                      |
| `semantico`       | Equivalência de comportamento, contratos, null-safety   |
| `seguranca`       | Autenticação, validação de inputs, superfície de ataque |
| `lint`            | Ruff determinístico + interpretação LLM das regressões  |
| `critico`         | Meta-revisor; aprova ou sinaliza `deve_reprocessar`     |
| `relatorio_final` | Consolida achados em Markdown                           |


Também expõe API FastAPI em `/review` e `/review/files`. Detalhes em [review_agent/README.md](review_agent/README.md).

**Saída:** `review_report.md` com veredito e lista de achados.

**Backend:** Groq via variável `API_3` (modelo `llama-3.3-70b-versatile`).

---

## Quick start

```bash
cd /path/to/agente-migracao-TALP
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r review_agent/requirements.txt
```

Crie `.env` na raiz (detalhes em [REPLICACAO.md](REPLICACAO.md#5-configurar-chaves-de-api-e-modelos)):

```env
GROQ_API_KEY=gsk_...
API_3=gsk_...
PROVIDER_API_KEY=gsk_...
PROVIDER_BASE_URL=https://api.groq.com/openai/v1
```

Execute o pipeline completo:

```bash
python test_pipeline.py
```

Opções úteis: `--skip-test`, `--skip-review`, `--input meu_codigo.py`, `--examples 5`.

---


## Documentação adicional

### Replicação e uso

| Arquivo                                          | Conteúdo                                            |
| ------------------------------------------------ | --------------------------------------------------- |
| [REPLICACAO.md](REPLICACAO.md)                   | Guia completo de replicação (Linux, macOS, Windows) |
| [PIPELINE.md](PIPELINE.md)                       | Detalhes do `test_pipeline.py`                      |
| [review_agent/README.md](review_agent/README.md) | API e arquitetura do review                         |
| [test_agent/README.md](test_agent/README.md)     | Thresholds e uso do test agent                      |

### Loop de auto-correção (migration ↔ review)

| Arquivo                                                        | Conteúdo                                                                 |
| ------------------------------------------------------------- | ------------------------------------------------------------------------ |
| [GUIA_LOOP_MIGRACAO_REVIEW.md](GUIA_LOOP_MIGRACAO_REVIEW.md)   | Passo a passo para conectar os dois agentes num laço (migra → revisa → refina), limitado a **3 rodadas ou até zerar P0/P1**. Orquestrador: `scripts/loop_review_migracao.py` |

### Análises e auditorias de código

| Arquivo                                                          | Conteúdo                                                                                  |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| [review_agent/INCONSISTENCIAS.md](review_agent/INCONSISTENCIAS.md) | Inconsistências/bugs do `review_agent` (módulo principal), do `eval/` e dos `prompts/`, com ideia de resolução |

### Avaliação

| Arquivo                                                                    | Conteúdo                                            |
| ------------------------------------------------------------------------- | --------------------------------------------------- |
| [AVALIACAO_SISTEMA.md](AVALIACAO_SISTEMA.md)                               | **Como avaliar o sistema como um todo** (pipeline + loop): métricas, protocolo, não-determinismo, validade |
| [review_agent/eval/README.md](review_agent/eval/README.md)                 | Avaliação isolada do review (Recall@R, StrictPass, pass@k) |
| [review_agent/AvaliacaoReviewAgent.md](review_agent/AvaliacaoReviewAgent.md) | Metodologia completa de avaliação                   |
| [review_agent/TestReviewStandalone.md](review_agent/TestReviewStandalone.md) | Como rodar o review isoladamente (Groq/Ollama)      |

### Dataset

| Arquivo                                                          | Conteúdo                                                                                       |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| [dataset/PILOTO_GROUND_TRUTH.md](dataset/PILOTO_GROUND_TRUTH.md) | Piloto de preenchimento de `all_code_before/after` + criação da coluna `ground truth` (P0–P3) |


---

