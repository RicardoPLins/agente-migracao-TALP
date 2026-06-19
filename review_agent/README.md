# CodeReviewAgent

Agente de revisão de migrações de código construído com **LangGraph** e **Groq** (`llama-3.3-70b-versatile`).

Analisa dois snapshots de código (original e migrado), gera um diff determinístico via `git diff --no-index`, aciona especialistas em paralelo e entrega um relatório consolidado em Markdown — **sem corrigir o código autonomamente**.

**Diretório:** `review_agent/` (referenciado como `REVIEW_AGENT_DIR` em `test_pipeline.py`).

Para instalação completa e replicação do pipeline integrado, consulte [REPLICACAO.md](../REPLICACAO.md) e [PIPELINE.md](../PIPELINE.md).

---

## Arquitetura

```
Entrada (original + migrado)
  │
  ▼
[no_parser]        → git diff --no-index + extração estrutural (Groq)
  │                  Produz: raw_diff + diff_estruturado (JSON)
  ▼
[no_classificador] → decide quais especialistas acionar (Groq)
  │                  agentes_acionados ⊆ {"semantica","seguranca","lint"}
  ▼
[no_roteador]      → fan-out paralelo via LangGraph Send()
  ├──► [no_semantico]  → equivalência funcional, contratos, null-safety
  ├──► [no_seguranca]  → autenticação, inputs, superfície de ataque
  └──► [no_lint]       → Ruff determinístico + interpretação LLM
            │
            ▼
       [no_critico]    → meta-revisor (Reflection Loop, até 3 iterações)
            │
            ├─ "aprovado" ou iteração = 3 → [relatorio_final] → END
            │
            └─ "requer_refinamento" → [no_roteador] (nova rodada)
```

### Nós e responsabilidades


| Nó                 | Função                                                               | LLM        |
| ------------------ | -------------------------------------------------------------------- | ---------- |
| `no_parser`        | `git diff --no-index` + diff estruturado (funções/classes/deps)      | Sim (Groq) |
| `no_classificador` | Roteia para especialistas com base no diff                           | Sim        |
| `no_roteador`      | Despacha via `Send()`; reseta achados a cada iteração                | —          |
| `no_semantico`     | Equivalência de comportamento, contratos, null-safety                | Sim        |
| `no_seguranca`     | Autenticação, validação de inputs, superfície de ataque              | Sim        |
| `no_lint`          | Ruff via `subprocess`; LLM interpreta regressões novas               | Sim        |
| `no_critico`       | Meta-avaliador; aprova ou pede refinamento                           | Sim        |
| `relatorio_final`  | Template Markdown determinístico (seções 2–6) + resumo executivo LLM | Parcial    |


Todos os nós com LLM usam `_get_llm()`:

```python
# Groq (default): REVIEW_LLM_PROVIDER=groq + API_3
# Ollama local:    REVIEW_LLM_PROVIDER=ollama + REVIEW_OLLAMA_MODEL=qwen2.5:7b
```

| Variável | Descrição | Default |
|----------|-----------|---------|
| `REVIEW_LLM_PROVIDER` | `groq` ou `ollama` | `groq` |
| `API_3` | Chave Groq | — (obrigatória se `groq`) |
| `REVIEW_GROQ_MODEL` | Modelo Groq | `llama-3.3-70b-versatile` |
| `REVIEW_OLLAMA_MODEL` | Modelo Ollama | `qwen2.5:7b` |
| `OLLAMA_BASE_URL` | API Ollama | `http://localhost:11434` |
| `REVIEW_OLLAMA_NUM_CTX` | Janela de contexto (VRAM) | (padrão Ollama) |

---

## Pré-requisitos

- Python **3.11+**
- Backend LLM: **Groq** (`API_3`) ou **Ollama** local (`REVIEW_LLM_PROVIDER=ollama`)
- [Ruff](https://docs.astral.sh/ruff/) no `PATH` (nó `no_lint`)
- `git` no `PATH` (nó `no_parser` — fallback para LLM se ausente)

---

## Instalação

Recomenda-se o venv compartilhado na **raiz do repositório** (`.venv/`):

```bash
cd agente-migracao-TALP
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r review_agent/requirements.txt
```

Alternativa isolada:

```bash
cd review_agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Configuração

Crie `.env` na raiz do repositório ou em `review_agent/`:

```env
API_3=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```


| Variável            | Descrição                                         | Obrigatória |
| ------------------- | ------------------------------------------------- | ----------- |
| `API_3`             | Chave Groq usada por `_get_llm()` em todos os nós | **Sim**     |
| `LANGSMITH_API_KEY` | Tracing via LangSmith                             | Não         |
| `LANGSMITH_PROJECT` | Nome do projeto no LangSmith                      | Não         |


> **Integração com `test_pipeline.py`:** o pipeline carrega `review_agent/review-agent.py` via `REVIEW_AGENT_DIR` e exige `API_3` antes de executar o review. Para o pipeline completo, configure também `GROQ_API_KEY` (migration) e `PROVIDER_`* (test). Ver [REPLICACAO.md](../REPLICACAO.md).

---

## Modos de uso

### Script de teste direto

**Linux / macOS:**

```bash
source .venv/bin/activate
python review_agent/testReviewAgent.py
```

**Windows (PowerShell):**

```powershell
python review_agent\testReviewAgent.py
```

Defina `API_3` no `.env` antes de executar. Usa `review_agent/test1/original.py` e `review_agent/test1/migrado.py`.

### API FastAPI

```bash
cd review_agent
uvicorn review-agent:app --host 127.0.0.1 --port 8000 --reload
```

Swagger: `http://127.0.0.1:8000/docs`

#### `POST /review` — JSON

```json
{
  "codigo_original": "<código urllib>",
  "codigo_migrado":  "<código requests>"
}
```

#### `POST /review/files` — upload de arquivos `.py`

Envie `codigo_original` e `codigo_migrado` via `multipart/form-data`.

### Via pipeline integrado

```bash
python test_pipeline.py              # review_agent/review-agent.py via import dinâmico
```

---

## Resposta (`_executar_grafo` / API)


| Campo                  | Tipo     | Descrição                                                |
| ---------------------- | -------- | -------------------------------------------------------- |
| `raw_diff`             | string   | Diff unificado do `git diff --no-index`                  |
| `diff`                 | object   | Diff estruturado (funções/classes alteradas)             |
| `agentes_acionados`    | string[] | `"semantica"`, `"seguranca"`, `"lint"`                   |
| `achados_semantica`    | string[] | Achados com severidade [P0]–[P3]                         |
| `achados_seguranca`    | string[] | Riscos de segurança [P0]–[P3]                            |
| `achados_lint`         | string[] | Regressões de lint/style                                 |
| `achados_estruturados` | object[] | Achados parseados (severidade, símbolo, linha corrigida) |
| `iteracoes`            | int      | Rodadas do reflection loop (máx. 3)                      |
| `deve_reprocessar`     | bool     | `true` → migration_agent deve refazer a migração         |
| `relatorio_final`      | string   | Relatório Markdown consolidado                           |


### Template do `review_report.md`

O nó `relatorio_final` **não** delega o relatório inteiro ao LLM. A estrutura é montada em Python (`_gerar_relatorio_markdown`):

1. **Legenda P0–P3** — tabela com significado e ação recomendada
2. **Resumo executivo** — única parte gerada pelo LLM (`relatorio_final.json`)
3. **Veredito** — APROVADO / APROVADO COM RESSALVAS / REPROVADO / REPROCESSAR
4. **Achados por severidade** — P0, P1, P2, P3 com emojis
5. **Detalhamento por agente** — semântica, segurança, lint
6. **Recomendações prioritárias** — contagem por severidade
7. **Notas sobre linhas** — explica a correção automática

### Correção de linhas (`achados_estruturados`)

A LLM frequentemente erra números de linha (especialmente ao copiar do hunk do diff). O pipeline:

1. Faz parse de cada achado (`[PREFIX][Px] \`funcao (line N) — …`)
2. Indexa `def nome()` no código migrado
3. Quando o símbolo existe, **substitui** a linha pela definição real
4. Anota *(modelo citou linha N)* quando houve divergência

Os achados corrigidos aparecem em `achados_semantica` / `achados_seguranca` / `achados_lint` (strings) e em `achados_estruturados` (JSON com `linha`, `linha_llm`, `linha_corrigida`).

Semântica e segurança recebem **trechos numerados** só das funções em `altered_functions` / `added_functions` (`<<trechos_migrados>>`), não o arquivo migrado inteiro — economia de tokens mantendo contexto local. O lint continua com `codigo_migrado` completo (Ruff + anti-patterns).

---

## Estrutura do diretório (`review_agent/`)

```
review_agent/
├── prompts/
│   ├── regras_migracao.txt          # Contrato fixo urllib→requests (injetado nos prompts)
│   ├── parser.json
│   ├── classificador.json
│   ├── agente_semantica.json
│   ├── agente_seguranca.json
│   ├── agente_lint_config.json
│   ├── agente_lint_interpretacao.json
│   ├── no_critico.json
│   └── relatorio_final.json
├── test1/
│   ├── original.py              # Exemplo urllib
│   ├── migrado.py               # Exemplo requests
│   └── ...
├── review-agent.py              # Grafo LangGraph + FastAPI + _executar_grafo()
├── testReviewAgent.py           # Teste standalone
├── requirements.txt
└── README.md
```

---

## Prompts — design

### Escala de severidade P0–P3


| Label                                    | Px    | Definição                              |
| ---------------------------------------- | ----- | -------------------------------------- |
| `[CONTRATO]`, `[NULL]`, `[COMPAT]`       | P0/P1 | Quebra funcional ou mudança silenciosa |
| `[AUTH-LOG]`, `[INPUT-SEC]`, `[SURFACE]` | P0/P1 | Risco de segurança                     |
| `[BLOCKER]`                              | P0    | Issue Ruff que quebra em runtime       |
| `[AVISO]`, `[TYPING-DRY]`                | P2    | Qualidade/manutenibilidade             |
| `[COSMÉTICO]`, `[NAMING]`                | P3    | Sugestão; não bloqueia                 |


### Placeholders por nó


| Arquivo                          | Placeholders                                                                                          |
| -------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `parser.json`                    | `<<codigo_original>>`, `<<codigo_migrado>>`, `<<raw_diff>>`                                           |
| `classificador.json`             | `<<diff_str>>`                                                                                        |
| `agente_semantica.json`          | `<<critica>>`, `<<diff_str>>`, `<<raw_diff>>`, `<<trechos_migrados>>`, `<<regras_migracao>>`          |
| `agente_seguranca.json`          | idem semântica                                                                                        |
| `agente_lint_config.json`        | `<<codigo_original>>`                                                                                 |
| `agente_lint_interpretacao.json` | `<<critica>>`, `<<novos_issues>>`, `<<estilo_inferido>>`, `<<codigo_migrado>>`, `<<regras_migracao>>` |
| `no_critico.json`                | `<<iteracao>>`, `<<achados_str>>`, `<<regras_migracao>>`                                              |
| `relatorio_final.json`           | `<<achados_resumo>>`, `<<deve_reprocessar>>` (apenas resumo executivo)                                |


`<<critica>>` é preenchido pelo `no_critico` nas iterações de refinamento; na primeira rodada fica vazio.

---

## Reflection loop

Constante `_MAX_ITERACOES = 3` em `review-agent.py`.

```
Iteração 1:
  no_roteador → [semantico + seguranca + lint em paralelo] → no_critico

  no_critico (LLM):
    · "aprovado"            → relatorio_final → END
    · "requer_refinamento"  → no_roteador (iteração 2)

Iteração 2–3:
  · Especialistas recebem <<critica>> com motivo_rejeicao do crítico
  · no_lint reexecuta Ruff (determinístico) a cada rodada

Iteração 3 (máxima):
  · Se LLM ainda retorna "requer_refinamento":
      status_qualidade = "aprovado" (força saída)
      deve_reprocessar = True
  · relatorio_final inclui aviso para o migration_agent refazer a migração
```

---

## Notas técnicas

- O agente **não corrige código** — apenas reporta achados acionáveis.
- `no_parser` prioriza `git diff --no-index` (determinístico); se `git` não estiver no PATH, o LLM compara os arquivos diretamente.
- `no_lint` executa Ruff via `subprocess`; o LLM só **interpreta** issues novos (não presentes no original).
- Prompts carregados **uma vez** na inicialização (`_load_prompts()`).
- Rate limits Groq: o `test_pipeline.py` aplica retry externo (45s × tentativa) ao chamar `_executar_grafo`; o review em standalone não tem retry interno.
- `deve_reprocessar = True` sinaliza ao `migration_agent` (via pipeline ou operador) que a migração deve ser refeita.

---

## Exemplo (API)

```bash
curl -X POST http://127.0.0.1:8000/review \
  -H "Content-Type: application/json" \
  -d '{
    "codigo_original": "import hashlib\n\ndef autenticar(senha, hash_bd):\n    return hashlib.md5(senha.encode()).hexdigest() == hash_bd\n",
    "codigo_migrado":  "import bcrypt\n\ndef autenticar(senha, hash_bd):\n    return bcrypt.checkpw(senha.encode(), hash_bd)\n"
  }'
```

---

## Documentação relacionada


| Arquivo                           | Conteúdo                          |
| --------------------------------- | --------------------------------- |
| [REPLICACAO.md](../REPLICACAO.md) | Setup completo e chaves de API    |
| [PIPELINE.md](../PIPELINE.md)     | Integração via `test_pipeline.py` |
| [README.md](../README.md)         | Visão geral dos três agentes      |


