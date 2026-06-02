# Avaliação do CodeReviewAgent

Metodologia para avaliar o agente de revisão de migração (`review-agent.py`) de forma **cientificamente defensável**, inspirada no artigo:

> **On Randomness in Agentic Evals** — Bjarnason, Silva & Monperrus (ICLR 2026 Workshop *Agents in the Wild*).  
> [arXiv:2602.07150](https://arxiv.org/abs/2602.07150)

Este documento explica **o que medir**, **como medir**, **quantas execuções são necessárias** e **como interpretar resultados** — em contraste com rodar `testReviewAgent.py` uma única vez e tirar conclusões.

---

## Sumário

1. [Por que avaliar de forma diferente](#1-por-que-avaliar-de-forma-diferente)
2. [O que é o agente sob teste](#2-o-que-é-o-agente-sob-teste)
3. [Definição de sucesso (gold labels)](#3-definição-de-sucesso-gold-labels)
4. [Métricas](#4-métricas)
5. [pass@k e pass^k adaptados](#5-passk-e-passk-adaptados)
6. [Protocolo experimental](#6-protocolo-experimental)
7. [Análise de trajetória e divergência](#7-análise-de-trajetória-e-divergência)
8. [Construção do benchmark](#8-construção-do-benchmark)
9. [Experimentos e ablações](#9-experimentos-e-ablações)
10. [Análise de poder estatístico](#10-análise-de-poder-estatístico)
11. [Erros comuns a evitar](#11-erros-comuns-a-evitar)
12. [Plano prático (primeiro experimento)](#12-plano-prático-primeiro-experimento)
13. [Exemplo ilustrativo: `test1`](#13-exemplo-ilustrativo-test1)
14. [Artefatos e próximos passos](#14-artefatos-e-próximos-passos)

---

## 1. Por que avaliar de forma diferente

O artigo demonstra, em 60.000 trajetórias no SWE-Bench-Verified, que:

- **Uma única execução por tarefa** produz estimativas de pass@1 com variância de **2,2 a 6,0 pontos percentuais** entre runs idênticas.
- Essa variância **persiste com temperature = 0** (não-determinismo de inferência, paralelismo, ambiente).
- Trajetórias **divergem cedo** (often nos primeiros ~1% dos tokens) e as diferenças **cascateiam** em estratégias distintas.

Implicação direta para este projeto:

| Prática comum | Problema |
|---------------|----------|
| Rodar `testReviewAgent.py` 1× | Mede **uma amostra**, não performance esperada |
| Comparar dois prompts com 1 run cada | Diferença de 2–3 achados pode ser **ruído** |
| Confiar só no resumo executivo (LLM) | Pode contradizer o veredito determinístico |
| Assumir `temperature=0` ⇒ reprodutível | Falso, segundo evidência empírica recente |

**Conclusão:** avaliar o review agent exige **múltiplas runs independentes**, **métricas com intervalo de confiança** e, idealmente, **gold labels** verificáveis por tarefa.

---

## 2. O que é o agente sob teste

No framework do artigo, **agente = modelo + scaffold + ambiente**.

### 2.1 Scaffold (grafo LangGraph)

Fluxo implementado em `review-agent.py`:

```
Entrada (original + migrado)
  │
  ▼
[no_parser]           git diff --no-index + LLM → diff_estruturado
  ▼
[no_classificador]    LLM → agentes_acionados ⊆ {semantica, seguranca, lint}
  ▼
[no_roteador]         fan-out via Send()
  ├── [no_semantico]  LLM
  ├── [no_seguranca]  LLM
  └── [no_lint]       Ruff (determinístico) + LLM
  ▼
[no_critico]          LLM — reflection; até _MAX_ITERACOES = 3
  │                     loop → no_roteador OU
  ▼
[relatorio_final]     template determinístico + resumo executivo LLM
```

### 2.2 Modelo e inferência

```python
ChatGroq(api_key=os.getenv("API_3"), model_name="llama-3.3-70b-versatile", temperature=0.0)
```

Variável de ambiente opcional futura: `REVIEW_GROQ_MODEL`.

### 2.3 Ambiente / ferramentas

| Componente | Determinístico? | Papel |
|------------|-----------------|-------|
| `git diff --no-index` | Sim | Fonte primária de mudanças |
| Ruff (`subprocess`) | Sim | Lint no código migrado |
| Prompts JSON (`prompts/`) | Sim | Instruções fixas |
| Groq API | **Não** (mesmo temp=0) | Todos os nós LLM |
| Fan-out paralelo (`Send`) | Parcial | Ordem/timing pode afetar rate limits |

### 2.4 Saída observável

`_executar_grafo()` retorna, entre outros:

- `relatorio_final` — Markdown humano
- `achados_semantica`, `achados_seguranca`, `achados_lint`
- `achados_estruturados` — achados parseados + linhas corrigidas
- `agentes_acionados`, `iteracoes`, `deve_reprocessar`

A **avaliação** deve operar sobre esses artefatos (e logs de trajetória), não só sobre impressão subjetiva do relatório.

---

## 3. Definição de sucesso (gold labels)

Diferente do SWE-Bench (testes unitários binários), o review produz **achados qualitativos**. É necessário um **conjunto gold** curado.

### 3.1 Por tarefa (par original + migrado)

Para cada item `i` do benchmark, anotar:

| Campo | Símbolo | Descrição |
|-------|---------|-----------|
| Regressões reais | `R_i` | Bugs/comportamentos **introduzidos** no diff (`+`) |
| Pré-existentes | `P_i` | Problemas já no original; **fora de escopo** da migração |
| Veredito esperado | `V*_i` | `APROVADO` \| `APROVADO_COM_RESSALVAS` \| `REPROVADO` |
| Severidade mínima | opcional | ex.: regressão HTTP → pelo menos P1 |

**Exemplo de entrada em JSON (conceitual):**

```json
{
  "id": "test1",
  "original": "review_agent/test1/original.py",
  "migrado": "review_agent/test1/migrado.py",
  "regressoes": [
    {
      "id": "http-no-raise",
      "simbolo": "executeRequest",
      "severidade": "P1",
      "descricao": "urllib.urlopen levanta HTTPError; requests.post não levanta 4xx/5xx por padrão"
    },
    {
      "id": "early-return",
      "simbolo": "scrapeConversation",
      "severidade": "P1",
      "descricao": "payload sem actions faz return; original faz pass e continua loop"
    }
  ],
  "preexistentes": [
    {
      "id": "json-load-no-try",
      "simbolo": "scrapeConversation",
      "descricao": "json.load sem try — igual no original e migrado"
    }
  ],
  "veredito_esperado": "APROVADO_COM_RESSALVAS"
}
```

### 3.2 Matching achado ↔ gold

Um achado detectado `d` **casa** com regressão `r ∈ R` se:

- `simbolo(d) == simbolo(r)` (ou overlap semântico definido), **e**
- severidade(d) ≥ severidade(r), **e**
- descrição/trigger cobre o aspecto anotado (manual ou via embedding/keywords).

Um achado que casa com `p ∈ P` conta como **falso positivo de migração** (mesmo que seja problema real de código).

---

## 4. Métricas

### 4.1 Nível tarefa (por run)

Seja `A_i` o conjunto de achados estruturados da run na tarefa `i`.

| Métrica | Fórmula / regra | Interpretação |
|---------|-----------------|---------------|
| **Recall@R** | `\|{ r ∈ R : ∃ d ∈ A_i que casa com r }\| / \|R\|` | Cobertura das regressões reais |
| **Precision@P1** | P1 em A que casam com R / P1 em A | Calibração de severidade alta |
| **FPR migração** | achados que casam com P / \|A\| | Ruído por problemas pré-existentes |
| **VereditoOK** | 1 se veredito == V*, else 0 | Acerto do veredito final |
| **StrictPass** | 1 se Recall@R=1 ∧ FPR baixo ∧ VereditoOK, else 0 | Sucesso estrito |

Limiares sugeridos para StrictPass:

- Recall@R ≥ 1,0 (todas regressões detectadas)
- Nenhum P1 falso (fora de R)
- VereditoOK = 1

### 4.2 Nível benchmark (agregado)

Com `N` tarefas e taxa de sucesso `s_i ∈ [0,1]` na tarefa `i` (ex.: StrictPass):

```
Score_run = (1/N) Σ s_i
```

Repita para cada run `j = 1..m` → obtém `r_1, ..., r_m`.

Reportar:

- **Média** `r̄` (equivalente a pass@1 pooled)
- **Desvio padrão** `σ`
- **Mín / máx** entre runs

### 4.3 Métricas por achado (diagnóstico)

| Métrica | Uso |
|---------|-----|
| Precision / Recall por severidade (P0, P1, …) | Calibração |
| % achados genéricos filtrados | Eficácia de `_validar_e_reclassificar_achado` |
| % runs com 3 agentes acionados | Cobertura do classificador |
| Iterações médias até aprovação | Custo do reflection loop |

---

## 5. pass@k e pass^k adaptados

O artigo define, para `N` tarefas e `m` runs por tarefa, com `c_i` = sucessos na tarefa `i`:

**pass@k** — probabilidade de sucesso em **pelo menos 1** de k tentativas (limite **otimista**):

```
pass@k = (1/N) Σ_i [ 1 - C(m-c_i, k) / C(m, k) ]
```

**pass^k** (pass∧k no artigo) — sucesso em **todas** as k tentativas (limite **pessimista**):

```
pass^k = (1/N) Σ_i [ C(c_i, k) / C(m, k) ]
```

Para k=1, pass@1 = pass^1 = média de taxa de sucesso por run.

### 5.1 Como interpretar no review agent

| Gap | Significado |
|-----|-------------|
| pass@5 >> pass@1 | Repetir o review **aumenta** chance de pegar regressões (explora variância) |
| pass@1 >> pass^5 | Sucesso **instável** — uma run boa não garante confiança |
| pass@1 ≈ pass^5 | Agente **consistente** (desejável para CI/CD) |

Plotar curvas pass@k e pass^k para k = 1..5 (como Fig. 1 do artigo) caracteriza o **envelope de performance**.

---

## 6. Protocolo experimental

### 6.1 Configuração fixa (documentar sempre)

- Modelo Groq e `temperature`
- Versão do código (`review-agent.py` commit hash)
- Versão dos prompts (`prompts/*.json`)
- `_MAX_ITERACOES`
- Conjunto de tarefas e gold labels (versão do benchmark)

### 6.2 Loop de avaliação

```
Para cada tarefa i = 1..N:
  Para cada run j = 1..m:          # m ≥ 10 recomendado
    result = _executar_grafo(original_i, migrado_i)
    salvar JSON + trajetória
    calcular s_i,j (StrictPass, Recall@R, ...)
Agregar r_j = (1/N) Σ s_i,j
Reportar r̄, σ, min, max
Calcular pass@k e pass^k
```

### 6.3 Número de runs (`m`)

| Objetivo | m sugerido |
|----------|------------|
| Estimativa exploratória | 5 |
| Comparar duas versões (Δ ≥ 2 pp, σ ≈ 1,5%) | **≥ 9** por condição |
| Detectar melhoria ~1 pp | **≥ 36** |
| Publicação / dissertação | 10–20 + intervalo de confiança |

### 6.4 Comparação entre versões (A vs B)

Duas condições (ex.: prompt v1 vs v2, com/sem pipeline de validação):

1. Mesmas tarefas, mesmo gold
2. `m` runs por condição
3. Teste t de duas amostras (ou bootstrap) em `r_j`
4. Reportar Δ = r̄_B − r̄_A com IC 95%
5. Só afirmar “melhoria” se p < 0,05 **e** Δ > σ

---

## 7. Análise de trajetória e divergência

### 7.1 Trajetória τ (adaptação)

Linearizar, por run, a sequência de interações:

```
τ = (prompt_parser, resp_parser) ⊕ (prompt_classificador, resp_classificador) ⊕ ...
    ⊕ (prompt_semantico, resp_semantico) ⊕ ... ⊕ (relatorio_final)
```

Incluir respostas de **ferramentas determinísticas** (stdout do Ruff, hash do git diff) como “tokens de ambiente” — eles condicionam nós LLM posteriores.

### 7.2 Primeira divergência τ_div

Para duas runs `a`, `b` na mesma tarefa:

```
τ_div(a,b) = primeiro nó (ou token) onde output_a ≠ output_b
```

**Hipóteses para este agente:**

1. Divergência frequente já em **`no_parser`** (JSON estruturado varia)
2. **`no_classificador`** muda `agentes_acionados` → cobertura diferente
3. **Reflection loop** amplifica diferenças iniciais (horizonte longo)

### 7.3 O que logar (implementação futura)

Por run, persistir:

```json
{
  "task_id": "test1",
  "run_id": 3,
  "nodes": [
    {"name": "no_parser", "prompt_sha256": "...", "response": "...", "latency_ms": 4200},
    {"name": "no_classificador", "...": "..."},
    ...
  ],
  "git_diff_sha256": "...",
  "ruff_issues_count": 12,
  "metrics": {"recall_R": 0.5, "strict_pass": 0}
}
```

---

## 8. Construção do benchmark

### 8.1 Tamanho e estratificação

| Camada | Conteúdo | Proporção sugerida |
|--------|----------|-------------------|
| Migração correta | R = ∅, V* = APROVADO | ~30% |
| 1 regressão P1 | ex.: HTTP, gzip, form body | ~40% |
| Múltiplas P0/P1 | combinações | ~20% |
| Sanity (regressão óbvia injectada) | controle positivo | ~10% |

Tamanho mínimo para claims gerais: **N ≥ 20–50** tarefas.

### 8.2 Fontes de tarefas

- `review_agent/test1/` — caso real ConversationScraper
- Saídas do `migration_agent` no pipeline (pares original/migrado)
- Migrações sintéticas com bug injectado conhecido
- Issues reais anotadas manualmente

### 8.3 Processo de anotação gold

1. Dois revisores independentes listam R e P
2. Consenso ou terceiro revisor
3. Versionar gold (`benchmark/gold/v1.json`)
4. Nunca usar gold no prompt (evitar vazamento)

---

## 9. Experimentos e ablações

Estruturar como matriz **modelo × scaffold × config**, espelhando o artigo:

| Eixo | Variantes | Isola |
|------|-----------|-------|
| **Reflection** | `_MAX_ITERACOES` = 1 vs 3 | Custo vs qualidade vs variância |
| **Validação** | com vs sem `_pipeline_achados` | Falsos positivos |
| **Roteamento** | classificador vs forçar `{semantica, seguranca, lint}` | Recall vs tokens |
| **Modelo** | llama-3.3-70b vs qwen3-32b | pass@1 e gap pass@k − pass^k |
| **Temperature** | 0.0 vs 0.2 | Estocasticidade extra |
| **Prompt** | versões de `agente_semantica.json`, etc. | Melhoria algorítmica real? |

Para cada célula da matriz: **m runs × N tarefas**, mesmas seeds de benchmark.

---

## 10. Análise de poder estatístico

Do artigo (σ = desvio entre runs, power = 80%, teste two-tailed):

| Melhoria Δ | σ = 0,7% | σ = 1,5% | σ = 1,8% |
|------------|----------|----------|----------|
| 1 pp | 8 runs | 36 runs | 51 runs |
| 2 pp | 2–3 runs | **9 runs** | 13 runs |
| 5 pp | 1 run | 2–3 runs | 3–4 runs |
| 10 pp | 1 run | 1 run | 1–2 runs |

**Nota:** o review agent provavelmente tem **σ maior** que agentes SWE-Bench (saída subjetiva, poucas tarefas). Na dúvida, use **m = 10** e reporte intervalos.

Fórmula geral (Apêndice A do artigo):

```
n ≥ 2 × ((Z_{α/2} + Z_β) × σ / Δ)²
```

---

## 11. Erros comuns a evitar

1. **Uma run = verdade** — invalidado pelo artigo e pela arquitetura multi-nó.
2. **Contar achados totais** — mais achados ≠ melhor agente.
3. **Ignorar pré-existentes (P)** — infla P1 e invalida StrictPass.
4. **Avaliar só o resumo executivo LLM** — seção 1 pode contradizer seções 2–6.
5. **Comparar modelos sem controlar tokens/cota** — 429 Groq introduz viés de truncamento.
6. **Benchmark sem gold** — impossível calcular recall/precision objetivos.
7. **Concluir melhoria com Δ < σ** — provavelmente ruído de avaliação.

---

## 12. Plano prático (primeiro experimento)

### Fase 0 — Piloto (1–2 dias)

- [ ] Anotar gold para `test1` (R, P, V*)
- [ ] Definir matching achado ↔ gold (manual)
- [ ] Rodar **m = 10** execuções de `testReviewAgent.py` (script em loop)
- [ ] Calcular Recall@R, StrictPass, r̄ ± σ
- [ ] Inspecionar variância entre runs

### Fase 1 — Harness mínimo (1 semana)

- [ ] Criar `review_agent/eval/` com:
  - `gold/v1.json`
  - `run_eval.py` — loop m × N, salva resultados
  - `score.py` — métricas + pass@k / pass^k
- [ ] Log de trajetória por nó (JSONL)

### Fase 2 — Benchmark (2–4 semanas)

- [ ] Expandir para N ≥ 20 tarefas
- [ ] Dois anotadores + consenso
- [ ] Curvas pass@k / pass^k
- [ ] Ablação: validação on/off, iter=1 vs 3

### Fase 3 — Comparação (dissertação / artigo)

- [ ] Comparar configurações com teste estatístico
- [ ] τ_div: onde as runs divergem
- [ ] Discussão alinhada a *On Randomness in Agentic Evals*

---

## 13. Exemplo ilustrativo: `test1`

Par: `test1/original.py` (urllib) vs `test1/migrado.py` (requests).

### Regressões plausíveis em R (gold sugerido)

| ID | Símbolo | Severidade | Evidência no diff |
|----|---------|------------|-------------------|
| http-no-raise | `executeRequest` | P1 | `urlopen` → `requests.post` sem `raise_for_status()` |
| early-return | `scrapeConversation` | P1 | `return` em erro vs `pass`/continue no original |
| form-body-ok | `generateRequestData` | — | dict + `data=` + Content-Type form → **correto** com requests |

### Pré-existentes em P (não penalizar como regressão)

- `json.load` / `json.loads` sem try (ambos os arquivos)
- `response[9:]` / `response.text[9:]` (mesmo padrão)
- `os.system` com concatenação (ambos)
- Exceções genéricas não tratadas em POST (ambos)

### Resultado esperado de uma eval rigorosa

Uma run que lista 4× P1 genéricos (JSONDecodeError, “may raise”, etc.) deve ter:

- **Recall@R baixo** (miss nos bugs reais de migração)
- **FPR alto** (P1 em problemas pré-existentes)
- **StrictPass = 0**

Isso calibra o benchmark: o agente atual pode parecer “rigoroso” no relatório, mas **fraco em recall de regressões reais**.

---

## 14. Artefatos e próximos passos

| Recurso | Descrição |
|---------|-----------|
| [TestReviewStandalone.md](./TestReviewStandalone.md) | Como executar o teste manual 1× |
| [README.md](./README.md) | Arquitetura do agente |
| [review-agent.py](./review-agent.py) | Grafo e pipeline de achados |
| [prompts/](./prompts/) | Templates LLM |
| [test1/](./test1/) | Par de exemplo |

**Harness implementado:**

```
review_agent/eval/
├── gold/v1.json
├── run_eval.py
├── score.py
├── README.md          ← guia de uso (somente review_agent)
└── results/
```

Ver [eval/README.md](./eval/README.md).

---

## Referências

- Bjarnason, B. H., Silva, A., & Monperrus, M. (2026). *On Randomness in Agentic Evals*. ICLR Workshop Agents in the Wild. [arXiv:2602.07150](https://arxiv.org/abs/2602.07150)
- Chen, M. (2021). *Evaluating Large Language Models Trained on Code* — pass@k original.
- Yao, S., et al. (2025). τ-bench — pass^k (consistência).
- Mustahsan, Z., et al. (2025). *Stochasticity in Agentic Evaluations* — ICC para estabilidade.

---

*Documento vivo: atualize gold labels e limiares conforme o benchmark amadurecer.*
