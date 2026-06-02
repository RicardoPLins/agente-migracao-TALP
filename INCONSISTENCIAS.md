# Inconsistências do `review_agent` — diagnóstico e plano de correção

Documento gerado a partir de uma revisão de código do módulo
[`review_agent/review-agent.py`](review-agent.py) e dos prompts em
[`review_agent/prompts/`](prompts/).

O `review_agent` é o agente que faz code review de código migrado de
`urllib` → `requests`. Ele recebe o código original e o migrado, roda um
grafo LangGraph (parser → classificador → roteador → agentes especialistas →
nó crítico → relatório) e entrega um relatório com achados **P0/P1/P2/P3**.
Deve rodar tanto localmente (Ollama) quanto na Cloud (Groq).

Severidade usada abaixo segue a mesma escala do produto:

| Nível | Significado |
|-------|-------------|
| **P0** | Quebra garantida em runtime (não roda / crash) |
| **P1** | Quebra provável / funcionalidade central morta |
| **P2** | Robustez, degradação, log enganoso |

---

## P0 — O módulo inteiro não importa: `ast` usado sem `import ast`

**Onde:** [`review-agent.py:635`](review-agent.py#L635) (e usos em
`visit_While`, `visit_Continue`, `visit_Return`, `_stats_loop_funcao`).

### O que aconteceu

A classe `_FluxoLoopFuncao(ast.NodeVisitor)` e a função `_stats_loop_funcao`
usam o módulo `ast` da biblioteca padrão (`ast.NodeVisitor`, `ast.parse`,
`ast.FunctionDef`, `ast.While`, …), mas **`ast` nunca é importado** no topo do
arquivo (linhas 17–39 só importam `json`, `os`, `re`, `subprocess`, `tempfile`,
`textwrap`, `threading`, `time`, `urllib.*`, `shutil`, `logging`, etc.).

O detalhe que engana: o arquivo começa com `from __future__ import annotations`.
Isso transforma as **anotações de tipo** (`node: ast.While`) em strings, então
elas não são avaliadas e não quebram. Porém a **expressão da classe-base**
`ast.NodeVisitor` na linha 635 **é avaliada no momento da importação** —
`from __future__ import annotations` não adia isso.

Resultado: ao importar o módulo (subir o `uvicorn`, rodar o pipeline, importar
em teste), o Python levanta imediatamente:

```
NameError: name 'ast' is not defined. Did you forget to import 'ast'?
```

Ou seja, **nada carrega**: nem a app FastAPI, nem o grafo, nem os checkers
determinísticos, nem o relatório final. Provavelmente o uso de `ast` (os
checkers determinísticos de regressão) foi adicionado em um commit recente sem
acrescentar o `import` correspondente — há `.pyc` em cache e saídas antigas em
`.pipeline_output/`, sugerindo que o módulo já funcionou antes dessa adição.

### Ideia de resolução

Adicionar `import ast` ao bloco de imports da stdlib:

```python
import ast
import json
import os
...
```

**Como prevenir no futuro:** rodar um smoke test de importação no CI
(`python -c "import importlib.util, sys; ..."` ou simplesmente
`python -c "import review_agent.review_agent"` após renomear o arquivo para um
nome importável) e/ou um `ruff check` com a regra `F821` (undefined name)
habilitada — o próprio Ruff teria apontado isso.

---

## P1 — O Reflection Loop nunca refina: chaves/valores PT × EN trocados

**Onde:** [`review-agent.py:1303`](review-agent.py#L1303) (`no_critico`) vs
[`prompts/no_critico.json`](prompts/no_critico.json).

### O que aconteceu

O nó crítico pede ao LLM um JSON e depois lê assim:

```python
decisao = resultado.get("decisao", "aprovado")          # chave PT
motivo  = resultado.get("motivo_rejeicao", "")          # chave PT
...
if state.get("iteracao", 0) >= _MAX_ITERACOES and decisao == "requer_refinamento":
```

Mas o prompt `no_critico.json` instrui o modelo a responder **em inglês**:

```json
{
  "decision": "approved" or "requires_refinement",
  "rejection_reason": "..."
}
```

Há **dois** desalinhamentos somados:

1. **Chave:** o código procura `decisao`/`motivo_rejeicao`; o prompt produz
   `decision`/`rejection_reason`. `resultado.get("decisao", ...)` nunca acha a
   chave e **cai sempre no default `"aprovado"`**.
2. **Valor:** mesmo que a chave batesse, o código compara com
   `"requer_refinamento"` / `"aprovado"`, enquanto o prompt gera
   `"requires_refinement"` / `"approved"`.

### Consequência

- `no_critico` **sempre aprova** na primeira passada.
- `motivo_rejeicao` fica sempre vazio → a crítica que deveria realimentar os
  agentes (`<<critica>>` em semântica/segurança/lint) nunca é preenchida.
- `deve_reprocessar` nunca vira `True` → o sinal de "refazer migração" não
  dispara.

Ou seja, **todo o Reflection Loop — o recurso central anunciado no docstring
do topo do arquivo — está morto.** O grafo vira um pipeline linear de uma
iteração só.

### Ideia de resolução

Padronizar a fronteira código↔prompt. A forma mais robusta (e consistente com
o resto do código) é **normalizar no código**, como já é feito para o
classificador em `_normalizar_agentes_classificador`:

```python
_ALIAS_DECISAO = {
    "approved": "aprovado",
    "aprovado": "aprovado",
    "requires_refinement": "requer_refinamento",
    "requer_refinamento": "requer_refinamento",
}

resultado = json.loads(_strip_md_fences(response.content))
bruto_decisao = (resultado.get("decision") or resultado.get("decisao") or "approved")
decisao = _ALIAS_DECISAO.get(str(bruto_decisao).strip().lower(), "aprovado")
motivo = resultado.get("rejection_reason") or resultado.get("motivo_rejeicao") or ""
```

Alternativa mais simples (mas mais frágil): trocar o prompt para devolver
`decisao`/`motivo_rejeicao` com valores `aprovado`/`requer_refinamento`. Não é
recomendado isoladamente porque LLMs tendem a "vazar" inglês mesmo quando
instruídos em PT — a normalização com aliases protege contra isso.

---

## P1 — `no_parser` quebra no fallback: variável `e` não definida

**Onde:** [`review-agent.py:391-396`](review-agent.py#L391-L396).

### O que aconteceu

```python
try:
    response = _invocar_llm(llm, prompt)
    diff = json.loads(_strip_md_fences(response.content))
except json.JSONDecodeError:
    logger.error(f"Falha na invocação do LLM ou no parsing do no_parser: {str(e)}")
    diff = {"raw": response.content, "parse_error": True}
```

Dois problemas:

1. O `except` captura `json.JSONDecodeError` **sem** `as e`, mas o log usa
   `{str(e)}`. Quando o JSON é inválido, em vez do fallback gracioso
   (`parse_error=True`), o Python levanta `NameError: name 'e' is not defined`
   e a revisão aborta.
2. O `try` também envolve `_invocar_llm`, mas o `except` só pega
   `JSONDecodeError`. Uma falha de rede/API (timeout do Groq, Ollama fora do ar)
   **não** é capturada e propaga sem fallback — e nesse caso `response` nem
   existe.

JSON inválido é um cenário **realista**, principalmente com modelos locais de
7B (Ollama), exatamente o modo de execução "local" que o projeto suporta.

### Ideia de resolução

```python
try:
    response = _invocar_llm(llm, prompt)
    diff = json.loads(_strip_md_fences(response.content))
except json.JSONDecodeError as e:
    logger.error(f"JSON inválido no no_parser: {e}")
    diff = {"raw": response.content, "parse_error": True}
except Exception as e:                      # falha de LLM/rede
    logger.error(f"Falha ao invocar LLM no no_parser: {e}", exc_info=True)
    diff = {"raw": "", "parse_error": True}
```

Assim o classificador ainda recebe `parse_error=True` e cai no fallback "aciona
todos os agentes", em vez de derrubar a revisão.

---

## P1 — `_run_ruff` retorna `None` em erro, mas quem chama espera `list`

**Onde:** [`review-agent.py:1157-1176`](review-agent.py#L1157-L1176) e
[`_filtrar_novos_issues`](review-agent.py#L1182).

### O que aconteceu

`_run_ruff` foi alterado para retornar `None` em vários caminhos de erro
(exit code ≠ 0/1, `TimeoutExpired`, `JSONDecodeError`, `FileNotFoundError`),
mas a docstring ainda promete `list[dict]` e os chamadores não foram
atualizados:

```python
issues_original = _run_ruff(state["codigo_original"], ruff_config)   # pode ser None
issues_migrado  = _run_ruff(state["codigo_migrado"],  ruff_config)   # pode ser None
novos_issues = _filtrar_novos_issues(issues_original, issues_migrado)
```

E `_filtrar_novos_issues` itera direto:

```python
presentes_original = Counter((i.get("code",""), ...) for i in issues_original)
```

Se qualquer um for `None` → `TypeError: 'NoneType' object is not iterable`, e o
nó de lint cai. Cenário realista: Ruff estourando o timeout de 30s em arquivos
grandes, ou saída não-JSON após um warning.

### Ideia de resolução

Escolher um contrato e aplicá-lo dos dois lados. O mais seguro é **nunca
retornar `None`** — manter o tipo `list` e sinalizar falha de outra forma
(log + lista vazia), já que lista vazia já significa "sem issues":

```python
except subprocess.TimeoutExpired:
    logger.error("Timeout de 30s no Ruff.")
    return []          # em vez de None
```

(ídem para os outros `except` e para o `returncode not in (0,1)`).

Se for importante distinguir "Ruff falhou" de "Ruff não achou nada", então
manter o `None` mas blindar o chamador:

```python
issues_original = _run_ruff(...) or []
issues_migrado  = _run_ruff(...) or []
```

e ajustar a anotação de retorno para `list[dict] | None`.

---

## P2 — `_run_git_diff`: `except` duplicado inalcançável + log trocado

**Onde:** [`review-agent.py:357-362`](review-agent.py#L357-L362).

### O que aconteceu

```python
except (subprocess.TimeoutExpired, FileNotFoundError):
    logger.warning("Timeout de 15s excedido ao executar git diff.")
    return None
except FileNotFoundError:
    logger.error("Binário 'git' não encontrado no PATH ...")
    return None
```

O primeiro `except` já captura `FileNotFoundError` (está na tupla), então o
segundo `except FileNotFoundError` é **código morto** — nunca executa. Pior:
se o `git` sumir do PATH em runtime, o operador vê a mensagem de **timeout**, e
a mensagem correta ("git não encontrado") jamais aparece. Diagnóstico
enganoso.

### Ideia de resolução

Separar os dois casos:

```python
except subprocess.TimeoutExpired:
    logger.warning("Timeout de 15s excedido ao executar git diff.")
    return None
except FileNotFoundError:
    logger.error("Binário 'git' não encontrado no PATH durante o _run_git_diff.")
    return None
```

(Nota: como `_verificar_dependencias_cli()` já exige `git` no boot, esse
caminho é raro — mas o log correto evita confusão se o ambiente mudar.)

---

## P2 — `indent_width` é inferido mas ignorado pelo Ruff

**Onde:** [`review-agent.py:1147`](review-agent.py#L1147) vs
[`_inferir_config_ruff`](review-agent.py#L1107).

### O que aconteceu

`_inferir_config_ruff` infere `indent_width` (via LLM), o coloca na config e
o exibe no relatório como "estilo inferido". Mas em `_run_ruff` a flag está
comentada:

```python
#f"--indent-width={config.get('indent_width', 4)}",
```

Então, para um código original com indentação de 2 espaços, o agente **diz**
que avaliou com `indent_width=2`, mas o Ruff rodou com o default. A promessa de
"avaliar a migração no mesmo padrão do legado" fica parcialmente falsa, e a
inferência do campo vira trabalho desperdiçado.

### Ideia de resolução

Decidir explicitamente:

- **Se a flag causava problema** (ex.: conflito com outra regra), remover
  também a inferência de `indent_width` da config e do relatório, para não
  prometer algo que não é aplicado.
- **Se foi comentário temporário**, reativar a flag:
  ```python
  f"--indent-width={config.get('indent_width', 4)}",
  ```

De qualquer forma, deixar config e execução coerentes (e remover o comentário
solto).

---

## Resumo / prioridade de correção

| # | Severidade | Item | Esforço |
|---|-----------|------|---------|
| 1 | **P0** | `import ast` ausente — módulo não importa | trivial (1 linha) |
| 2 | **P1** | Reflection loop morto (chaves/valores PT×EN no `no_critico`) | baixo |
| 3 | **P1** | `NameError` em `no_parser` (`e` não definido) + erro de LLM não capturado | baixo |
| 4 | **P1** | `_run_ruff` retorna `None` → `TypeError` no filtro | baixo |
| 5 | **P2** | `except` duplicado/inalcançável em `_run_git_diff` | trivial |
| 6 | **P2** | `indent_width` inferido mas não aplicado | trivial |

**Ordem sugerida:** corrigir #1 primeiro (sem ele nada roda), depois #2–#4
(que afetam a correção/robustez do produto em uso normal e nos modelos locais),
e por fim #5–#6.

**Recomendação geral:** habilitar `ruff check` com `F` (Pyflakes — pega `F821`
nome indefinido, como o `ast` e o `e`) no CI; #1, #3 e parte de #4 teriam sido
detectados automaticamente.

### Observações (NÃO são bugs)

- O **classificador** também devolve chaves/valores em inglês
  (`{"agents": ["semantics", "security", "lint"]}`), mas isso **já é tratado**
  por `_normalizar_agentes_classificador` (aceita PT e EN). Consistente.
- O **parser** usa chaves em inglês (`altered_functions`, etc.) dos dois lados.
  Consistente.

---
---

# Inconsistências do `review_agent/eval`

Revisão focada na pasta de avaliação:
[`eval/run_eval.py`](eval/run_eval.py) (runner CLI) e
[`eval/score.py`](eval/score.py) (métricas), avaliados contra
[`eval/gold/v1.json`](eval/gold/v1.json).

O eval roda N execuções independentes do `review_agent` por tarefa, pontua
contra labels gold (regressões esperadas + problemas pré-existentes) e gera
`Recall@R`, `StrictPass`, `pass@k`/`pass^k`. Foco da análise: **bugs,
inconsistências e falta de tratamento de exceções.**

---

## E1 — P1 — Uma run que falha aborta o eval inteiro e perde o agregado

**Onde:** [`eval/run_eval.py:141`](eval/run_eval.py#L141) (chamada a
`run_single` → `_executar_grafo`), agregação em
[`eval/run_eval.py:159-165`](eval/run_eval.py#L159-L165).

### O que aconteceu

`run_single` chama `mod._executar_grafo(original, migrado)` sem nenhum
`try/except`. Como o grafo de review faz várias chamadas a LLM (Groq/Ollama),
uma falha transitória (HTTP 429, timeout, queda de rede, JSON inválido não
tratado a montante) levanta exceção que sobe por `run_single` → `main` →
`SystemExit`, **matando o processo inteiro**.

Pior: o `aggregate.json`, o `summary.md` e o `aggregate_task_runs` só são
escritos **depois** do loop de runs (linhas 159–165). Então, se a falha ocorre
na run 5 de 10:

- as runs 6–10 nunca rodam;
- o agregado das runs 1–4 (já bem-sucedidas) **nunca é gravado**.

Isso anula o propósito do eval, que é justamente medir **variância entre N
runs** (`pass@k`, `pass^k`, `r_std`) — um único soluço de API descarta tudo.

### Ideia de resolução

Isolar cada run e degradar graciosamente:

```python
try:
    payload = run_single(mod, gold_task, run_id)
except Exception as e:
    logger.error("Run %d da task %s falhou: %s", run_id, task_id, e, exc_info=True)
    payload = {"task_id": task_id, "run_id": run_id, "erro": str(e),
               "achados_estruturados": [], "agentes_acionados": []}
    exit_code = 1
```

E garantir que o agregado seja computado sobre as runs disponíveis (idealmente
gravando incrementalmente ou num `finally` por task), para não perder trabalho
já feito.

---

## E2 — P1 — Import do módulo de review sem tratamento (falha opaca)

**Onde:** [`eval/run_eval.py:42`](eval/run_eval.py#L42)
(`spec.loader.exec_module(mod)`).

### O que aconteceu

`_load_review_module` carrega `review-agent.py` via `importlib` e executa
`exec_module(mod)` sem `try/except`. Qualquer erro de importação do módulo
revisado sobe como traceback cru apontando para dentro de `review-agent.py`.

**Hoje isso está ativo:** por causa do bug **P0** (`import ast` ausente),
rodar `python review_agent/eval/run_eval.py --task test1` falha imediatamente
em `exec_module` com `NameError: name 'ast' is not defined` — o usuário do eval
recebe um erro confuso, sem indicação de que o problema é o módulo de review e
não o eval. O mesmo valeria para qualquer `ImportError`/`SyntaxError` futuro.

### Ideia de resolução

Encapsular com mensagem clara:

```python
try:
    spec.loader.exec_module(mod)
except Exception as e:
    raise RuntimeError(
        f"Falha ao carregar review-agent.py ({type(e).__name__}: {e}). "
        "Verifique imports/sintaxe do módulo de review."
    ) from e
```

(E corrigir o **P0** do `import ast`, que é a causa atual.)

---

## E3 — P2 — `_read_pair` não trata arquivo ausente (inconsistente)

**Onde:** [`eval/run_eval.py:47-48`](eval/run_eval.py#L47-L48).

### O que aconteceu

`_read_pair` faz `(REVIEW_DIR / task["original"]).read_text(...)` e idem para
`migrado`, sem tratar `FileNotFoundError`. Se uma task do gold apontar para um
caminho errado (typo ao ampliar o benchmark — algo que o README incentiva), o
eval inteiro morre.

Isso é **inconsistente** com o tratamento de `task_id` inexistente, que é
gracioso (linhas 120–123):

```python
if task_id not in gold["tasks"]:
    print(f"ERRO: tarefa '{task_id}' não está em {args.gold}")
    exit_code = 1
    continue
```

Dois erros de input do mesmo tipo (gold malformado) têm comportamentos opostos:
um pula a task e segue; o outro derruba tudo.

### Ideia de resolução

Tratar a leitura como erro recuperável por task: capturar
`FileNotFoundError`/`OSError`, imprimir o caminho problemático, setar
`exit_code = 1` e `continue` para a próxima task — alinhando com o tratamento
de `task_id` inválido.

---

## E4 — P2 — `recall=0` silencioso em drift de schema

**Onde:** [`eval/score.py:89`](eval/score.py#L89)
(`achados = result.get("achados_estruturados") or []`).

### O que aconteceu

`score_task` lê `result.get("achados_estruturados") or []`. Se um `run_NN.json`
antigo (em `--score-only`) não tiver essa chave, ou se `_executar_grafo` deixar
de emiti-la, `achados` vira `[]` **silenciosamente** → `recall_R=0`,
`regressoes_detectadas=[]`, `strict_pass=False`.

Ou seja: uma **incompatibilidade de formato** fica indistinguível de uma
**regressão real de qualidade do agente**. O relatório parece dizer "o agente
piorou" quando na verdade o schema mudou. Nenhum log diferencia os casos.

### Ideia de resolução

Distinguir explicitamente "sem achados" de "chave ausente":

```python
if "achados_estruturados" not in result:
    logger.warning("Run sem 'achados_estruturados' — schema antigo? Score não confiável.")
achados = result.get("achados_estruturados") or []
```

(ou marcar o score como inválido/`None` quando a chave não existir).

---

## E5 — P2 — Lógica de veredito duplicada (eval × produto)

**Onde:** [`eval/score.py:73`](eval/score.py#L73) (`infer_veredito`) vs
[`review-agent.py`](review-agent.py#L1509) (`_veredito`).

### O que aconteceu

`infer_veredito` reimplementa a regra de veredito (REPROCESSAR / REPROVADO /
APROVADO_COM_RESSALVAS / APROVADO) que **já existe** em `_veredito` no módulo de
review. São duas fontes de verdade para a mesma decisão.

Se a regra mudar no produto (ex.: novo limiar para REPROCESSAR, ou P2 passar a
influenciar), `_veredito` muda e `infer_veredito` **não** — o eval passa a
medir `veredito_ok` contra uma regra obsoleta e reporta `StrictPass` errado,
sem ninguém perceber.

### Ideia de resolução

O eval já importa o módulo de review (`mod`). Reusar a função do produto em vez
de duplicar — ex.: expor um helper puro `veredito_de_counts(counts, deve_reprocessar)`
em `review-agent.py` e chamá-lo tanto no relatório quanto no `infer_veredito`
do eval.

---

## E6 — P3 — `load_gold` colapsa ids de task duplicados em silêncio

**Onde:** [`eval/score.py:20`](eval/score.py#L20).

### O que aconteceu

```python
tasks = {t["id"]: t for t in data.get("tasks", [])}
```

Se duas tasks tiverem o mesmo `id` (copiar-colar ao ampliar o benchmark — caso
explicitamente sugerido no README), a dict-comprehension mantém só a última. A
task duplicada **desaparece do benchmark sem aviso**, alterando as médias.

### Ideia de resolução

Detectar e alertar:

```python
ids = [t["id"] for t in data.get("tasks", [])]
dups = [i for i, c in Counter(ids).items() if c > 1]
if dups:
    raise ValueError(f"IDs de task duplicados no gold: {dups}")
```

---

## E7 — P3 — `main` não trata gold inexistente / JSON inválido

**Onde:** [`eval/run_eval.py:108`](eval/run_eval.py#L108)
(`gold = load_gold(args.gold)`).

### O que aconteceu

`--gold caminho/errado.json` (ou um `v1.json` com vírgula sobrando) faz
`read_text`/`json.loads` levantarem `FileNotFoundError`/`JSONDecodeError` com
traceback cru, em vez de uma mensagem de CLI amigável + exit code.

### Ideia de resolução

Capturar em `main` e sair com `print(erro) + return 1`.

---

## E8 — P3 — `format_report` rotula runs pelo índice, não pelo `run_id`

**Onde:** [`eval/run_eval.py:188-189`](eval/run_eval.py#L188-L189)
(`for i, s in enumerate(per_run): ... f"- run {i}: ..."`).

### O que aconteceu

O relatório usa o índice do `enumerate` como rótulo da run, mas em
`--score-only` algumas runs podem ter sido puladas (AVISO). Com run_00 e
run_02 presentes e run_01 ausente, o relatório imprime "run 0" e "run 1",
enquanto os arquivos/scores reais são run_00 e run_02. O leitor associa a
métrica à run errada.

### Ideia de resolução

Usar o identificador real: `f"- run {s['run_id']}: ..."`.

---

## Resumo / prioridade — `eval`

| # | Severidade | Item | Tipo |
|---|-----------|------|------|
| E1 | **P1** | Run que falha aborta o eval e perde agregado | exceção |
| E2 | **P1** | Import do módulo de review sem guarda (falha opaca; hoje ativa pelo P0) | exceção |
| E3 | **P2** | `_read_pair` não trata arquivo ausente (incoerente com task_id) | exceção/inconsistência |
| E4 | **P2** | `recall=0` silencioso em drift de schema | bug/métrica enganosa |
| E5 | **P2** | Veredito duplicado entre eval e produto | inconsistência |
| E6 | **P3** | `load_gold` descarta ids duplicados em silêncio | bug latente |
| E7 | **P3** | `main` sem tratamento de gold inválido | exceção |
| E8 | **P3** | Rótulo de run pelo índice e não pelo `run_id` | inconsistência cosmética |

**Ordem sugerida:** E1 e E2 primeiro (robustez do runner — sem elas o eval é
frágil/inutilizável), depois E3–E5 (correção de métrica e coerência), por fim
E6–E8.

### Observações do eval (NÃO são bugs)

- `pass_at_k` / `pass_all_k` estão **corretos**, incluindo os ramos
  `math.comb(n<k) → 0` e os guards `total_runs < k`.
- `_severidade_ok` trata `severidade_min` corretamente (P0 satisfaz mínimo P1).
- `cobertura_3_agentes` é coerente com `REVIEW_FORCE_ALL_AGENTS=1` setado em
  `_load_review_module`.

---
---

# Análise dos prompts (`review_agent/prompts/`)

Revisão dos templates de prompt usados pelos nós do grafo:
[`parser.json`](prompts/parser.json), [`classificador.json`](prompts/classificador.json),
[`agente_semantica.json`](prompts/agente_semantica.json),
[`agente_seguranca.json`](prompts/agente_seguranca.json),
[`agente_lint_config.json`](prompts/agente_lint_config.json),
[`agente_lint_interpretacao.json`](prompts/agente_lint_interpretacao.json),
[`no_critico.json`](prompts/no_critico.json), [`relatorio_final.json`](prompts/relatorio_final.json),
e os contratos [`regras_migracao.txt`](prompts/regras_migracao.txt) /
[`rubrica_evidencia.txt`](prompts/rubrica_evidencia.txt).

**Avaliação geral:** prompts bem construídos — calibração de confiança estilo
PR-Agent, rubrica de evidência objetiva, checklists de auto-verificação e
contrato urllib→requests explícito. Os pontos abaixo são inconsistências entre
o que o prompt **promete ao modelo** e o que o **código faz**, além de
desperdício e polish.

## Inconsistências prompt ↔ código (impacto funcional)

### PR1 — P1 — `no_critico.json` responde em inglês; o código lê português

Mesmo problema do **P2** da Parte 1 (Reflection Loop), visto pelo lado do prompt.
O template instrui:

```json
{ "decision": "approved" or "requires_refinement", "rejection_reason": "..." }
```

mas o código lê `decisao`/`motivo_rejeicao` e compara com `"aprovado"`/
`"requer_refinamento"`. → o nó crítico sempre aprova.

**Ideia de resolução:** preferencialmente corrigir no código (aliases EN/PT,
como já existe em `_normalizar_agentes_classificador`). Alternativa só no
prompt: alinhar a saída para `{"decisao": "aprovado"|"requer_refinamento",
"motivo_rejeicao": ...}`.

### PR2 — P2 — `parser.json` promete recálculo determinístico que não existe

O prompt afirma:

> *"added_dependencies and removed_dependencies will be recalculated
> deterministically from import statements after your response"*

Não há esse recálculo no código — as dependências vêm 100% do LLM e alimentam
direto `_agentes_por_diff_deterministico` ([`review-agent.py:452-453`](review-agent.py#L452-L453)).
A decisão de acionar o agente de segurança depende de um campo que o prompt diz
ser determinístico mas não é.

**Ideia de resolução:** ou **remover a NOTE** (para não enganar o modelo), ou
**implementar** o recálculo de fato (regex `^\s*(import|from)\s+` sobre o diff).
Implementar torna o roteamento de segurança confiável.

### PR3 — P2 — `agente_lint_interpretacao.json` delega tipos a um mypy que não roda

O prompt diz duas vezes que mypy cobre tipos:

> *"mypy type errors are handled separately (deterministic) — do NOT re-report
> mypy findings"* e *"mypy may also catch attr-defined separately"*.

**mypy nunca é executado** no código (só Ruff). Logo, o modelo é instruído a
**não** reportar erros de tipo / `attr-defined`, mas ninguém os pega — inclusive
o anti-pattern (c) "wrong module alias after library swap" fica descoberto.

**Ideia de resolução:** ou **adicionar mypy** como tool determinístico (espelho
do `_run_ruff`), ou **reescrever a instrução** para o LLM verificar ativamente
atributos/módulos inexistentes, em vez de delegar a um mypy ausente.

## Eficiência / cleanup

### PR4 — P2 — `agente_seguranca.json` injeta `<<regras_migracao>>` duas vezes

O contrato (~1 KB) é duplicado no mesmo prompt (2 ocorrências, contra 1 nos
demais). Desperdício de tokens em **toda** chamada do agente de segurança —
relevante dado o limite de TPM do Groq free que o código tenta contornar com
rate-limit.

**Ideia de resolução:** remover a segunda ocorrência.

## Polish / consistência

### PR5 — P3 — Legenda de severidade do STEP 1 do lint pula P1

A legenda inline do STEP 1 lista `[P0]`, `[P2]`, `[P3]` mas omite `[P1]`, embora
P1 esteja definido no topo e na lista de prefixos válidos.

**Ideia de resolução:** incluir `[WARNING][P1]` na legenda do STEP 1.

### PR6 — P3 — Mistura de idiomas nos achados

Prompts em inglês, `critica` injetada em português (`⚠️ CRÍTICA DA ITERAÇÃO
ANTERIOR`), relatório em português. O LLM pode devolver `descricao` ora em EN
ora em PT, e isso vai direto para o relatório.

**Ideia de resolução:** padronizar o idioma dos achados (sugiro PT, coerente com
o relatório final e o público).

### PR7 — P3 — `no_critico.json` usa enum ambíguo

`"decision": "approved" or "requires_refinement"` parece sintaxe, mas é texto
livre — ambíguo para o modelo.

**Ideia de resolução:** trocar por enum explícito: *"decision deve ser
exatamente um de: approved | requires_refinement"*.

## Resumo / prioridade — prompts

| # | Severidade | Item | Tipo |
|---|-----------|------|------|
| PR1 | **P1** | `no_critico` EN×PT (reflection loop) | inconsistência |
| PR2 | **P2** | `parser` promete recálculo de deps inexistente | inconsistência |
| PR3 | **P2** | `lint` delega tipos a mypy que não roda | lacuna de cobertura |
| PR4 | **P2** | `seguranca` duplica `regras_migracao` | eficiência |
| PR5 | **P3** | Legenda de severidade do lint sem P1 | polish |
| PR6 | **P3** | Mistura EN/PT nos achados | consistência |
| PR7 | **P3** | `no_critico` enum ambíguo | polish |

### Observações dos prompts (NÃO são problemas)

- O formato dos achados (`- [PREFIX][Px] \`símbolo\` (linha N) — … Trigger: …`)
  está **alinhado** com a regex `ACHADO_RE`, incluindo a linha opcional e os
  travessões `—–-`.
- `<<regras_migracao>>` / `<<rubrica_evidencia>>` são injetados automaticamente
  via `setdefault` em `_render`, então não ficam como placeholder literal mesmo
  quando o nó não os passa explicitamente.
- As convenções de numeração de linha (`coluna | código`) batem com o que
  `_extrair_trechos_funcoes` gera.
