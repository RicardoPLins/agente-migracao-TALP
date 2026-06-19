# Avaliação isolada do `review_agent`

Esta pasta avalia **somente** o agente em `review_agent/` (`REVIEW_AGENT_DIR`).

**Não** roda `migration_agent`, **não** roda `test_agent`, **não** usa `test_pipeline.py`.

---

## Pré-requisitos

- Dependências: `pip install -r review_agent/requirements.txt`
- Backend LLM configurado no `.env` da raiz (Groq ou Ollama) — ver [TestReviewStandalone.md](../TestReviewStandalone.md)
- `git` e `ruff` no PATH (mesmos requisitos do grafo de review)

---

## Estrutura

```
review_agent/eval/
├── gold/v1.json       # Labels de referência (regressões R, pré-existentes P)
├── run_eval.py        # Executa N runs + pontua
├── score.py           # Métricas (Recall@R, StrictPass, pass@k)
├── results/           # Saídas (gerado ao rodar)
└── README.md
```

---

## Uso rápido

Na **raiz do repositório**:

```powershell
# 1 run em test1 ( consome LLM )
python review_agent/eval/run_eval.py --task test1 --runs 1

# 3 runs independentes (para estimar variância / pass@k)
python review_agent/eval/run_eval.py --task test1 --runs 3

# Re-pontuar runs já salvas (zero tokens)
python review_agent/eval/run_eval.py --task test1 --score-only
```

Saídas em `review_agent/eval/results/test1/`:

| Arquivo | Conteúdo |
|---------|----------|
| `run_00.json` | Resultado bruto do `_executar_grafo` |
| `run_00.score.json` | Métricas da run |
| `aggregate.json` | pass@1, pass@3, média Recall@R, etc. |
| `summary.md` | Relatório legível |

---

## Métricas

| Métrica | Significado |
|---------|-------------|
| **Recall@R** | Fração das regressões gold detectadas |
| **falsos_p1_preexistente** | P0/P1 em problemas que já existiam no original |
| **veredito_ok** | Veredito inferido == gold |
| **StrictPass** | Recall@R=1 ∧ sem falso P1 ∧ veredito_ok |
| **pass@k / pass^k** | Envelope otimista/pessimista (artigo *On Randomness in Agentic Evals*) |

---

## Ampliar o benchmark

Edite `gold/v1.json`:

```json
{
  "id": "minha_tarefa",
  "original": "test1/original.py",
  "migrado": "caminho/relativo/a/review_agent/migrado.py",
  "veredito_esperado": "APROVADO_COM_RESSALVAS",
  "regressoes": [ { "id": "...", "simbolo": "foo", "severidade_min": "P1", "keywords": ["..."] } ],
  "preexistentes": [ { "id": "...", "simbolo": "bar", "keywords": ["..."] } ]
}
```

Paths `original` / `migrado` são relativos a `review_agent/`.

---

## Cota de tokens (Groq)

Cada run em `test1` ≈ **20–40k tokens**. Para m=10 runs, espalhe em vários dias ou use Ollama local.

Metodologia completa: [AvaliacaoReviewAgent.md](../AvaliacaoReviewAgent.md)
