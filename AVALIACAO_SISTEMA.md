# Como avaliar o sistema agente como um todo

Guia de metodologia para avaliar o pipeline completo de migração
(`migration_agent` → `test_agent` → `review_agent`) e o **loop de auto-correção**
(`scripts/loop_review_migracao.py`), não apenas cada agente isolado.

> Avaliação **isolada do review** já existe em
> [`review_agent/eval/`](review_agent/eval/README.md). Este documento mostra
> como medir o **sistema inteiro** e como reaproveitar aquele harness.

---

## 1. O que significa "avaliar o sistema como um todo"

Três níveis, do mais barato ao mais caro:

| Nível | Pergunta que responde | Custo |
|-------|------------------------|-------|
| **Unitário (por agente)** | Cada agente faz bem o seu trabalho isolado? | baixo |
| **Integração (pares)** | migração→review e migração→test conversam bem? | médio |
| **End-to-end (loop)** | O sistema **converge** para uma migração correta sem intervenção humana? | alto |

A avaliação só é confiável com **dados de referência (gold)** e medindo sobre
**várias execuções** (LLM é não-determinístico — ver §7).

---

## 2. Dados de referência (gold / ground truth)

Duas fontes complementares:

1. **`review_agent/eval/gold/v1.json`** — gold do review: por tarefa lista
   `regressoes` (o que o agente DEVE achar, com `simbolo`/`severidade_min`/`keywords`)
   e `preexistentes` (o que NÃO deve virar P0/P1). Usado pelo `score.py`.

2. **`dataset/Request-Urllib-Codigo-Inteiro.xlsx`** — migrações reais com
   `all_code_before` / `all_code_after` e a coluna **`ground truth`** (lista
   P0–P3 do que a migração introduziu). Ver
   [dataset/PILOTO_GROUND_TRUTH.md](dataset/PILOTO_GROUND_TRUTH.md).

> ⚠️ **Validade — contaminação treino/teste:** o `migration_agent` usa
> `dataset/Request-Urllib.xlsx` como few-shot. Garanta que os pares usados na
> **avaliação** não estejam no conjunto de **exemplos** do migrador (mesmo repo
> ⇒ vazamento). Mantenha um split explícito.

---

## 3. Métricas por dimensão

### 3.1 Migração (qualidade do código migrado)

| Métrica | Definição | Como medir |
|---------|-----------|------------|
| **Equivalência funcional** | comportamento preservado (original ≈ migrado) | `test_agent` (testes de equivalência) |
| **Validação estática** | sem `urllib` legado, com `requests`, sintaxe ok | `no_validar_migracao` do migration_agent |
| **Regressões introduzidas** | nº de P0/P1 que a migração criou | `review_agent` + comparar com `ground truth` |
| **Compilabilidade** | `python -m py_compile` no migrado | subprocess |

### 3.2 Review (detecção de problemas) — reusa `eval/score.py`

| Métrica | Definição |
|---------|-----------|
| **Recall@R** | fração das regressões gold detectadas |
| **falsos_p1_preexistente** | P0/P1 reportados em problemas pré-existentes (falso positivo) |
| **veredito_ok** | veredito inferido == gold (`APROVADO` / `APROVADO_COM_RESSALVAS` / `REPROVADO` / `REPROCESSAR`) |
| **StrictPass** | `Recall@R == 1` ∧ `falsos_p1 == 0` ∧ `veredito_ok` |
| **Precisão (P0/P1)** | dos achados P0/P1, fração que casa com regressões gold |

Definindo precisão/recall sobre o `ground truth` da planilha:
```
TP = achados P0/P1 que casam com um item do ground truth (símbolo + keyword)
FP = achados P0/P1 que não casam (e batem com pré-existente)
FN = itens do ground truth P0/P1 não detectados
Recall    = TP / (TP + FN)
Precisão  = TP / (TP + FP)
F1        = 2·P·R / (P + R)
```

### 3.3 Test agent (geração de testes)

| Métrica | Definição |
|---------|-----------|
| **Cobertura** | % de linhas/funções exercidas (limiar do projeto ≈ 80%) |
| **Equivalência** | % de cenários equivalentes (limiar ≈ 90%) |
| **Detecção de não-equivalência** | quando o migrado QUEBRA, o test acusa? (recall do teste) |

### 3.4 Loop end-to-end (o coração da avaliação do sistema)

Usa o `loop_resultado.json` (histórico por rodada com contagem P0–P3) de
`scripts/loop_review_migracao.py`.

| Métrica | Definição | Por quê importa |
|---------|-----------|-----------------|
| **Taxa de convergência** | % de casos que terminam `aprovado` (P0/P1 = 0) | mede se o loop resolve sozinho |
| **Iterações até convergir** | nº de rodadas até zerar P0/P1 | eficiência do laço |
| **ΔP0/P1 por rodada** | redução de P0/P1 entre rodadas consecutivas | o refino melhora ou piora? |
| **P0/P1 residual** | P0/P1 restantes ao parar (limite atingido) | qualidade do "melhor esforço" |
| **Taxa de regressão do loop** | casos onde uma rodada AUMENTA P0/P1 | o refino quebrou algo? |
| **Equivalência final** | test_agent no código final | o loop não pode "consertar" o review quebrando o comportamento |
| **Custo** | tokens/tempo por caso | viabilidade (limite TPM do Groq) |

> ⚠️ **Métrica de "aprovado" não basta sozinha.** O loop pode zerar P0/P1
> apenas removendo funcionalidade. Por isso **sempre** cruze convergência com
> **equivalência funcional** (test_agent) e com o `ground truth`.

---

## 4. Protocolo de execução (passo a passo)

### 4.1 Avaliação isolada do review (já pronta)
```bash
# m runs por tarefa para estimar variância / pass@k
python review_agent/eval/run_eval.py --task test1 --runs 5
python review_agent/eval/run_eval.py --score-only        # re-pontuar sem gastar LLM
```
Saídas em `review_agent/eval/results/<task>/`: `run_NN.score.json`,
`aggregate.json` (pass@k/pass^k), `summary.md`.

### 4.2 Avaliação end-to-end do loop (a construir — ver §9)
Para cada par `(all_code_before, all_code_after?)` do dataset:
```bash
# roda o loop a partir do código ORIGINAL (não do migrado humano)
python scripts/loop_review_migracao.py --input <original.py> --max-iteracoes 3
```
Depois pontue o `loop_resultado.json` + `codigo_migrado_final.py` contra a
coluna `ground truth`:
- Recall/precisão dos P0/P1 detectados na 1ª rodada (qualidade do review).
- Convergência e iterações (qualidade do loop).
- Equivalência funcional do código final (test_agent).

### 4.3 Pipeline completo (migração→test→review)
```bash
python test_pipeline.py --input <original.py>
```
Use para medir integração e produzir artefatos em `.pipeline_output/`.

---

## 5. Montando a base de avaliação

1. Selecione N pares do `Request-Urllib-Codigo-Inteiro.xlsx` **fora** do few-shot
   do migrador (split treino/teste).
2. Garanta `all_code_before` preenchido (entrada do loop) e `ground truth`
   revisado (idealmente por humano — o piloto atual é gerado por IA).
3. Formalize cada caso no schema do `gold/v1.json` (regressões + pré-existentes)
   para pontuar com `score.py` sem reescrever métricas.
4. Versione o gold (`v1`, `v2`, …) — toda mudança de label muda os números.

---

## 6. Determinismo e o que fixar

- `temperature=0` nos três agentes (já é o default) reduz, mas **não elimina**,
  a variância.
- Fixe versões de modelo (`REVIEW_GROQ_MODEL`, `MIGRATION_OLLAMA_MODEL`).
- Registre no relatório: modelo, provider, data, versão do gold, nº de runs.

---

## 7. Lidando com não-determinismo (m runs, pass@k)

Rode **m execuções** por caso e reporte envelopes (já implementado em
`eval/score.py`):

- **pass@k** — otimista: prob. de ≥1 sucesso em k amostras.
- **pass^k** — pessimista: prob. de sucesso em TODAS as k amostras.
- **média ± desvio** de Recall@R e StrictPass.

Referência: *On Randomness in Agentic Evals* (citado no `eval/README.md`).
Sugestão: m ≥ 5 (idealmente 10), espalhando no tempo se usar Groq free (limite
de TPM).

---

## 8. Armadilhas de validade

| Armadilha | Efeito | Mitigação |
|-----------|--------|-----------|
| **Circularidade** | usar a saída do review como gold do próprio review | gold independente (humano), nunca o próprio agente |
| **Contaminação treino/teste** | repo de avaliação está no few-shot do migrador | split explícito por repo |
| **"Aprovado" enganoso** | loop zera P0/P1 removendo funcionalidade | cruzar com equivalência (test_agent) |
| **Bug EN/PT do nó crítico** | veredito interno do review e `deve_reprocessar` ficam furados | o loop conta P0/P1 direto de `achados_estruturados` (robusto); corrigir o review para o `veredito_ok` valer (ver [INCONSISTENCIAS.md](review_agent/INCONSISTENCIAS.md)) |
| **Drift de schema** | run antiga sem `achados_estruturados` → recall 0 silencioso | validar schema (ver E4 em INCONSISTENCIAS.md) |
| **Repos mortos (404)** | casos do dataset não reproduzíveis | marcar e excluir do denominador |
| **Ground truth gerado por IA** | labels não confiáveis | revisão humana antes de virar gold oficial |

---

## 9. O que ainda falta construir (roadmap)

O harness de avaliação **end-to-end do loop** ainda não existe como script
único. Para tê-lo, criar `scripts/eval_loop.py` que:

1. Lê o `Request-Urllib-Codigo-Inteiro.xlsx` (split de teste).
2. Para cada caso: roda `loop_review_migracao.executar_loop` a partir do
   `all_code_before`.
3. Pontua contra a coluna `ground truth` (recall/precisão P0/P1 da 1ª review).
4. Coleta métricas de convergência do `historico` (iterações, ΔP0/P1, residual).
5. Roda o `test_agent` no `codigo_migrado_final.py` (equivalência).
6. Agrega com `eval/score.py` (pass@k) + grava `summary.md` e `aggregate.json`.

Reaproveite: `review_agent/eval/score.py` (métricas), o padrão de m-runs do
`run_eval.py`, e o tratamento de exceção por caso (E1–E3 do eval).

---

## 10. O que reportar (modelo de relatório)

```
# Avaliação do sistema — <data>
Modelo: <provider/modelo>  | Gold: v<N>  | Runs: m=<k>  | Casos: <n> (split teste)

## Por agente
- Migração: equivalência <%>, regressões introduzidas <média>, compila <%>
- Review:   Recall@R <μ±σ>, Precisão P0/P1 <%>, falsos_P1 <média>, veredito_ok <%>
- Test:     cobertura <%>, detecção de não-equivalência <%>

## Loop end-to-end
- Convergência (aprovado): <%>
- Iterações até convergir: <média> (de máx 3)
- P0/P1 residual ao parar: <média>
- Regressões do loop (rodada que piorou): <nº casos>
- Equivalência do código final: <%>
- Custo médio: <tokens> / <segundos> por caso

## Envelopes (não-determinismo)
- pass@1 / pass@3 / pass@5 e pass^k por caso

## Ameaças à validade
- <contaminação, repos 404 excluídos, ground truth IA-vs-humano, etc.>
```

---

## Referências internas

- [review_agent/eval/README.md](review_agent/eval/README.md) — avaliação isolada do review
- [review_agent/AvaliacaoReviewAgent.md](review_agent/AvaliacaoReviewAgent.md) — metodologia detalhada do review
- [dataset/PILOTO_GROUND_TRUTH.md](dataset/PILOTO_GROUND_TRUTH.md) — base com `ground truth`
- [GUIA_LOOP_MIGRACAO_REVIEW.md](GUIA_LOOP_MIGRACAO_REVIEW.md) — o loop a ser avaliado
- [review_agent/INCONSISTENCIAS.md](review_agent/INCONSISTENCIAS.md) — bugs que afetam a avaliação
