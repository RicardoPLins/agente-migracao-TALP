# Test Equivalence Agent

LangGraph agent that auto-generates and runs equivalence tests between
an original urllib-based implementation and its requests-based migration.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your ANTHROPIC_API_KEY
```

## Usage

```bash
python agent/agent.py path/to/original.py path/to/migrated.py --output report.md
```

## Pipeline

```
Entrada (JSON) → Analisador → Gerador → Executor → Avaliador → Relatório
                                  ↑___________↓ (loop se cobertura < 80%)
```

| Nó | Prompt | Responsabilidade |
|---|---|---|
| Analisador | `node1_analyzer.txt` | Mapeia funções, endpoints, cenários |
| Gerador | `node2_generator.txt` | Gera `test_equivalence.py` com pytest |
| Executor | — (subprocess) | Roda pytest + pytest-cov |
| Avaliador | `node4_evaluator.txt` | Métricas, decisão CONTINUE/FINALIZE |
| Relatório | `node_report.txt` | Markdown report final |

## Thresholds (configurável em agent.py)

| Parâmetro | Default |
|---|---|
| `COVERAGE_THRESHOLD` | 80% |
| `EQUIVALENCE_THRESHOLD` | 90% |
| `MAX_ITERATIONS` | 3 |

## Output

O relatório final em Markdown contém:
- Taxa de equivalência (passed / total)
- Cobertura de linhas por módulo
- Regressões detectadas
- Score geral (0–10)
- Recomendação: APPROVED / CONDITIONAL / REJECTED

## Avaliação offline do próprio agente

Para medir se o agente detecta regressões injetadas:

```python
# Injete um bug no código migrado e rode o agente
# Score de detecção = bugs_detectados / bugs_injetados
```