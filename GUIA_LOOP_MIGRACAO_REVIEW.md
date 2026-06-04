# Guia — Loop de auto-correção `migration_agent` ↔ `review_agent`

Passo a passo para **conectar** o `migration_agent` ao `review_agent` num laço
que migra `urllib → requests`, revisa o resultado e **refina a migração
aplicando o relatório**, parando **em 3 rodadas ou quando não restarem erros
P0/P1**.

Este guia serve tanto para entender o que já foi implementado quanto para
**refazer do zero** caso necessário.

---

## 1. Visão geral

```
            ┌──────────────────────────────────────────────┐
            │                  ORQUESTRADOR                 │
            │        scripts/loop_review_migracao.py        │
            └──────────────────────────────────────────────┘
                              │
   código urllib ─────────────┘
        │
        ▼
  [migration_agent] migra ──► código migrado v0
        │                          │
        │                          ▼
        │                   [review_agent] revisa (original × migrado)
        │                          │  achados P0/P1/P2/P3
        │                          ▼
        │                   P0 ou P1 > 0 ?
        │            ┌──────────────┴───────────────┐
        │           SIM (e rodada < 3)             NÃO  → APROVADO ✅
        │            │                                 (entrega o código)
        │            ▼
        └──[migration_agent] REFINA com o relatório ──► código migrado vN
                     (volta para o review)

  Limite: 3 rodadas de review  →  LIMITE_ATINGIDO ⚠️ (entrega melhor esforço)
```

**Regra de parada (o que vier primeiro):**
1. **3 rodadas de review** (configurável), ou
2. **0 achados P0/P1** → migração aprovada.

O código final entregue é **sempre o último que passou pela revisão**.

---

## 2. Pré-requisitos

### Ferramentas no PATH
- `git` — usado pelo review (`git diff --no-index`).
- `ruff` — usado pelo review (lint determinístico). **Sem ele o review nem
  importa** (`_verificar_dependencias_cli()` levanta `RuntimeError`).

### Dependências Python
```bash
pip install -r review_agent/requirements.txt
pip install langgraph langchain-groq langchain-ollama langchain-core \
            fastapi pydantic openpyxl python-dotenv
pip install ruff            # se ainda não estiver instalado
```

### Backend LLM (um dos dois)
- **Groq (cloud):** variável `API_3` no `.env` (review) e chave do migration.
- **Ollama (local):** `REVIEW_LLM_PROVIDER=ollama` + modelo baixado
  (ex.: `ollama pull qwen2.5:7b`).

---

## 3. Passo a passo da implementação

### Passo 0 — Corrigir o pré-requisito de import do review *(obrigatório)*

O `review_agent/review-agent.py` usa `ast` (classe base `ast.NodeVisitor`) mas
não importava o módulo → `NameError` no import. Adicionar no bloco de imports:

```python
from __future__ import annotations

import ast          # ← necessário; sem isso o módulo não importa
import json
import os
import re
...
```

> Verifique com: `python -c "import ast"` e, no ambiente completo, importando o
> próprio módulo (ver Passo 3).

### Passo 1 — Entender as interfaces dos dois agentes

**`review_agent/review-agent.py`** expõe:
```python
_executar_grafo(codigo_original: str, codigo_migrado: str) -> dict
```
Retorna, entre outros:
- `achados_estruturados`: lista de dicts com `severidade` (`"P0"`..`"P3"`),
  `prefixo`, `simbolo`, `descricao`, `trigger`, `formatado`.
- `relatorio_final`: relatório Markdown.
- `deve_reprocessar`: bool.

**`migration_agent/langgraph-mig03.py`** tem o estado `EstadoAgente` e os nós
`no_migrar_com_llm` (migra) e `no_refinar_com_feedback` (aplica `feedback_revisao`).
A flag `WRITE_ARTIFACTS = __name__ == "__main__"` garante que **importar** o
módulo não escreve arquivos.

### Passo 2 — Adaptar o `migration_agent` (API pública para o loop)

Adicionar antes do bloco `if __name__ == "__main__":` três wrappers finos sobre
os nós existentes (sem alterar o grafo nem o comportamento do `__main__`):

```python
def preparar_contexto_migracao(num_exemplos: int = 10) -> tuple[list[dict], str]:
    """Carrega exemplos few-shot + prompt-sistema UMA vez (reutilizado no loop)."""
    exemplos = carregar_exemplos_treino(num_exemplos)
    prompt_sistema = criar_prompt_treino(exemplos)
    return exemplos, prompt_sistema


def migrar_codigo(codigo_usuario, exemplos_treino, prompt_sistema) -> str:
    """Migração inicial urllib→requests."""
    resultado = no_migrar_com_llm(
        {"codigo_usuario": codigo_usuario, "codigo_migrado": "",
         "feedback_revisao": "", "status": ""},
        exemplos_treino, prompt_sistema)
    return resultado.get("codigo_migrado", "") or ""


def aplicar_feedback_revisao(codigo_usuario, codigo_migrado, feedback,
                             exemplos_treino, prompt_sistema) -> str:
    """Refino incremental: aplica o relatório do review ao código migrado."""
    resultado = no_refinar_com_feedback(
        {"codigo_usuario": codigo_usuario, "codigo_migrado": codigo_migrado,
         "feedback_revisao": feedback, "status": "migrado"},
        exemplos_treino, prompt_sistema)
    return resultado.get("codigo_migrado", codigo_migrado) or codigo_migrado
```

> Os nós usam `estado.get(...)`, então aceitam dicts de estado parciais.

### Passo 3 — Carregar os módulos (nomes de arquivo com hífen)

`review-agent.py` e `langgraph-mig03.py` têm hífen → não dá `import` direto.
Usar `importlib`:

```python
import importlib.util
from pathlib import Path

def _carregar_modulo(nome, caminho):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # aqui dispara _verificar_dependencias_cli()
    return mod
```

### Passo 4 — Escrever o laço (orquestrador)

Lógica essencial (já implementada em `scripts/loop_review_migracao.py`):

```python
MAX_ITERACOES = 3   # limite de rodadas de review

exemplos, prompt = mig.preparar_contexto_migracao(10)
migrado = mig.migrar_codigo(original, exemplos, prompt)   # v0

for iteracao in range(1, MAX_ITERACOES + 1):
    review_out = rev._executar_grafo(original, migrado)
    achados = review_out.get("achados_estruturados") or []
    p0 = sum(1 for a in achados if a.get("prefixo") and a.get("severidade") == "P0")
    p1 = sum(1 for a in achados if a.get("prefixo") and a.get("severidade") == "P1")

    if p0 + p1 == 0:                 # parada nº 2: sem P0/P1 → aprovado
        status = "aprovado"; break
    if iteracao == MAX_ITERACOES:    # parada nº 1: limite de rodadas
        status = "limite_atingido"; break

    feedback = montar_feedback(review_out)        # relatório + P0/P1 explícitos
    migrado = mig.aplicar_feedback_revisao(original, migrado, feedback, exemplos, prompt)
```

**Detalhes importantes:**
- A contagem de P0/P1 vem **direto de `achados_estruturados`**, não do veredito
  interno do nó crítico (que tem um bug EN/PT — ver seção 6).
- Com `MAX_ITERACOES = 3`: até **3 reviews** e até **2 refinos**. Toda
  refatoração é seguida de uma nova review → o código final é sempre verificado.
- O feedback enviado ao migration inclui o relatório completo **+** a lista
  explícita dos achados P0/P1 (campo `formatado`), para focar o refino.

---

## 4. Como executar

```bash
# padrão: lê url.py, máx. 3 rodadas
python scripts/loop_review_migracao.py --input url.py

# ajustando o limite e o nº de exemplos few-shot
python scripts/loop_review_migracao.py --input url.py --max-iteracoes 3 --exemplos 10
```

**Saídas** em `.loop_output/`:
| Arquivo | Conteúdo |
|---------|----------|
| `codigo_migrado_final.py` | código migrado final (último revisado) |
| `relatorio_final.md` | relatório da última review |
| `loop_resultado.json` | status + histórico por rodada (contagem P0–P3) |

**Exit codes:** `0` = aprovado · `3` = limite atingido com pendências ·
`1` = erro na migração · `2` = erro de uso (input ausente, etc.).

---

## 5. Como validar sem gastar LLM

Como o loop depende de LLM (Groq/Ollama), valide a lógica com **mocks** dos dois
módulos (ver o teste usado no desenvolvimento):

```python
class MigMock:
    def preparar_contexto_migracao(self, n): return ([], "PROMPT")
    def migrar_codigo(self, *a): return "v0"
    def aplicar_feedback_revisao(self, *a): return "v1"

class RevMock:                 # rodada 1 com P1, rodada 2 limpa
    def __init__(self): self.n = 0
    def _executar_grafo(self, o, m):
        self.n += 1
        sev = "P1" if self.n == 1 else "P3"
        return {"achados_estruturados": [{"prefixo": "X", "severidade": sev, "formatado": "f"}],
                "relatorio_final": "r"}
```

Cenários a cobrir: **aprovado** (P0/P1 zera antes do limite) e
**limite_atingido** (nunca zera → para em 3 rodadas). Confirme também:
`py_compile` nos arquivos alterados.

---

## 6. Pegadinhas / troubleshooting

- **`NameError: name 'ast'` ao importar o review** → faltou o Passo 0.
- **`RuntimeError: FALTA DE DEPENDÊNCIAS CRÍTICAS`** → instale `git`/`ruff` no PATH.
- **`ModuleNotFoundError: langgraph/langchain/fastapi`** → instale as deps (seção 2).
- **Loop não converge / sempre `limite_atingido`** → o nó crítico do review tem
  um bug EN/PT (lê `decisao`/`aprovado` mas o prompt devolve `decision`/`approved`),
  então o veredito interno e o `deve_reprocessar` ficam furados. O **orquestrador
  não depende disso** (conta P0/P1 direto), mas vale corrigir o review para o
  relatório ficar coerente (ver `review_agent/INCONSISTENCIAS.md`, item P1 #2).
- **Refino “re-migra do zero”?** Não: `aplicar_feedback_revisao` usa
  `no_refinar_com_feedback`, que parte do **código migrado atual** + feedback —
  refino incremental, não reinício.
- **Não confunda com `scripts/run_pipeline_with_feedback.py`** — aquele importa
  um pacote `agents.*` inexistente (quebrado) e usa outra interface de review.
  O orquestrador deste guia conecta os módulos **reais**.

---

## 7. Arquivos envolvidos

| Arquivo | Papel | Mudança |
|---------|-------|---------|
| `review_agent/review-agent.py` | revisor (P0–P3) | `+ import ast` |
| `migration_agent/langgraph-mig03.py` | migrador/refinador | `+ preparar_contexto_migracao / migrar_codigo / aplicar_feedback_revisao` |
| `scripts/loop_review_migracao.py` | **orquestrador do laço** | arquivo novo |

---

## 8. Parâmetros e onde ajustar

| O quê | Onde | Default |
|-------|------|---------|
| Limite de rodadas | `--max-iteracoes` / `_MAX_ITERACOES_DEFAULT` | 3 |
| Exemplos few-shot | `--exemplos` | 10 |
| Acionar os 3 especialistas sempre | `--force-all-agents` (env `REVIEW_FORCE_ALL_AGENTS=1`) | ligado |
| Arquivo de entrada | `--input` | `url.py` |
| Diretório de saída | `--output-dir` | `.loop_output/` |
| Backend LLM | `.env` (`API_3` / `REVIEW_LLM_PROVIDER=ollama`) | Groq |
