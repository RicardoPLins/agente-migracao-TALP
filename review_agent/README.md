# CodeReviewAgent

Agente de revisão de migrações de código construído com **LangGraph** e **Groq (LLaMA 3.3 70B)**.

O agente analisa dois snapshots de código (original e migrado), gera um diff determinístico via `git diff`, aciona especialistas em paralelo e entrega um relatório consolidado em Markdown — **sem corrigir o código autonomamente**.

---

## Arquitetura

```
Entrada (original + migrado)
  │
  ▼
[no_parser]        → diff via git diff --no-index + extração estrutural via LLM
  │                  Produz: raw_diff (unified diff) + diff_estruturado (JSON)
  ▼
[no_classificador] → decide quais especialistas acionar com base no diff
  │                  Produz: agentes_acionados ⊆ {"semantica","seguranca","lint"}
  ▼
[no_roteador]      → fan-out paralelo via LangGraph Send()
  ├──► [no_semantico]  → equivalência funcional, contratos de API, null-safety
  ├──► [no_seguranca]  → autenticação, inputs, superfície de ataque, headers
  └──► [no_lint]       → executa Ruff determinístico; LLM interpreta regressões
            │
            ▼
       [no_critico]    → meta-revisor (Reflection Loop, máx 3 iterações)
            │
            ├─ "aprovado" ou iteração = 3
            │        ▼
            │  [relatorio_final]  → Markdown consolidado → END
            │
            └─ "requer_refinamento"
                     ▼
               [no_roteador]  (nova rodada com crítica do no_critico inclusa)
```

### Nós e responsabilidades

| Nó | Função |
|---|---|
| `no_parser` | Gera `raw_diff` via `git diff --no-index` e extrai diff estruturado (funções/classes/deps alteradas) via LLM |
| `no_classificador` | Roteia para os especialistas relevantes com base em padrões do diff (nomes de funções, imports, etc.) |
| `no_roteador` | Despacha tarefas em paralelo via `Send()` do LangGraph; reseta achados a cada iteração |
| `no_semantico` | Verifica equivalência de comportamento, contratos de API, semântica de null, compatibilidade retroativa |
| `no_seguranca` | Verifica autenticação/logging, validação de inputs, superfície de ataque, dependências com CVEs |
| `no_lint` | Executa Ruff via `subprocess`; filtra issues pré-existentes; LLM interpreta apenas regressões novas |
| `no_critico` | Avalia qualidade dos achados; aprova ou devolve crítica estruturada para refinamento |
| `relatorio_final` | Consolida todos os achados em um relatório Markdown com veredito final |

---

## Pré-requisitos

- Python **3.9+** (recomendado 3.10+)
- Conta e chave de API na [Groq](https://console.groq.com/)
- [Ruff](https://docs.astral.sh/ruff/) acessível no `PATH` (instalado via `pip install ruff` ou standalone)
- `git` no `PATH` (usado pelo `no_parser` para o diff determinístico)

---

## Instalação

```bash
# Na pasta review_agent/
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Configuração

Crie um arquivo `.env` dentro de `review_agent/`:

```env
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Variáveis opcionais:

| Variável | Descrição |
|---|---|
| `GROQ_API_KEY` | **Obrigatória** — chave de API da Groq |
| `LANGSMITH_API_KEY` | Habilita tracing via LangSmith |
| `LANGSMITH_PROJECT` | Nome do projeto no LangSmith |

---

## Modos de uso

### Modo API (FastAPI)

```bash
# Dentro de review_agent/, com o venv ativo:
uvicorn review-agent:app --host 127.0.0.1 --port 8000 --reload
```

Swagger UI disponível em `http://127.0.0.1:8000/docs`

#### `POST /review` — revisão via JSON

```json
{
  "codigo_original": "<código urllib>",
  "codigo_migrado":  "<código requests>"
}
```

#### `POST /review/files` — revisão via upload de arquivos

Envie dois arquivos `.py` via `multipart/form-data`. Ideal para testes no Swagger UI.

### Modo direto (sem FastAPI)

A função `_executar_grafo(original, migrado)` pode ser chamada diretamente:

```python
import sys
sys.path.insert(0, "review_agent")
import importlib.util, os

os.environ["GROQ_API_KEY"] = "gsk_..."

spec = importlib.util.spec_from_file_location("review_agent", "review_agent/review-agent.py")
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)

result = ra._executar_grafo(original_code, migrated_code)
print(result["relatorio_final"])
```

---

## Resposta da API

| Campo | Tipo | Descrição |
|---|---|---|
| `raw_diff` | string | Diff unificado gerado pelo `git diff --no-index` |
| `diff` | object | Diff estruturado (funções/classes alteradas/adicionadas/removidas) |
| `agentes_acionados` | string[] | Especialistas chamados: `"semantica"`, `"seguranca"`, `"lint"` |
| `achados_semantica` | string[] | Problemas de equivalência semântica |
| `achados_seguranca` | string[] | Riscos de segurança identificados |
| `achados_lint` | string[] | Regressões de lint/style introduzidas pela migração |
| `iteracoes` | int | Rodadas do Reflection Loop (máx 3) |
| `deve_reprocessar` | bool | `true` quando no_critico sinaliza que a migração deve ser refeita |
| `relatorio_final` | string | Relatório Markdown consolidado com veredito final |

---

## Estrutura do Projeto

```
review_agent/
├── prompts/
│   ├── parser.json                   # Extração de diff estruturado + uso de raw_diff
│   ├── classificador.json            # Roteamento granular com categorias de skills
│   ├── agente_semantica.json         # Equivalência funcional + calibração de confiança
│   ├── agente_seguranca.json         # Segurança + calibração de confiança
│   ├── agente_lint_config.json       # Inferência de config Ruff a partir do código original
│   ├── agente_lint_interpretacao.json# Interpretação de regressões Ruff + calibração
│   ├── no_critico.json               # Meta-revisor (Reflection Loop)
│   └── relatorio_final.json          # Consolidação final
├── test1/
│   ├── original.py                   # Código urllib de exemplo (ConversationScraper)
│   ├── migrado.py                    # Versão migrada para requests
│   ├── chunkOriginal.py              # Chunk menor do original
│   ├── chunkMigrado.py               # Chunk menor migrado
│   ├── saida.txt                     # Saída de execução completa
│   └── saidaChunk.txt                # Saída de execução com chunks
├── .venv/                            # Ambiente virtual Python
├── review-agent.py                   # Orquestrador LangGraph + API FastAPI
├── requirements.txt
└── README.md
```

---

## Prompts — Design e Evolução

### Fontes de inspiração integradas

Os prompts foram construídos com técnicas derivadas de duas fontes externas:

1. **[awesome-reviewers](https://github.com/baz-scm/awesome-reviewers)** — biblioteca de skills de revisão de código. Skills incorporadas:

| Arquivo | Skills incorporadas |
|---|---|
| `agente_semantica.json` | `Preserve API Contracts`, `Preserve Null Semantics`, `Maintain Backwards Compatibility` |
| `agente_seguranca.json` | `Secure Auth and Logging`, `Validate Security Inputs` |
| `agente_lint_interpretacao.json` | `Code Style, Typing & DRY`, `Self-Documenting Naming Rules` |
| `classificador.json` | Todas as anteriores — usadas como categorias de roteamento |

2. **[pr-agent (Qodo)](https://github.com/Codium-ai/pr-agent)** — técnicas de `pr_reviewer_prompts.toml`:
   - Calibração de confiança (Alta / Média / Baixa) com critérios explícitos
   - Campo `Trigger:` obrigatório — cada achado precisa descrever *quando* se manifesta
   - Anti-especulação — proibição de reportar achados sem evidência direta no diff
   - Referência de linha obrigatória: `(linha N do migrado)`

### Placeholders por nó

| Arquivo | Placeholders |
|---|---|
| `parser.json` | `<<codigo_original>>`, `<<codigo_migrado>>`, `<<raw_diff>>` |
| `classificador.json` | `<<diff_str>>` |
| `agente_semantica.json` | `<<critica>>`, `<<diff_str>>`, `<<raw_diff>>`, `<<codigo_original>>`, `<<codigo_migrado>>` |
| `agente_seguranca.json` | `<<critica>>`, `<<diff_str>>`, `<<raw_diff>>`, `<<codigo_original>>`, `<<codigo_migrado>>` |
| `agente_lint_config.json` | `<<codigo_original>>` |
| `agente_lint_interpretacao.json` | `<<critica>>`, `<<novos_issues>>`, `<<estilo_inferido>>`, `<<codigo_migrado>>` |
| `no_critico.json` | `<<iteracao>>`, `<<achados_str>>` |
| `relatorio_final.json` | `<<achados_str>>`, `<<diff_str>>` |

O placeholder `<<critica>>` é preenchido automaticamente pelo `no_critico` nas iterações de refinamento; na primeira rodada é string vazia.

---

## Comportamento do Reflection Loop

```
Iteração 1:
  no_roteador → [semantico + seguranca + lint em paralelo] → no_critico
  no_critico avalia qualidade:
    - SE "aprovado" → relatorio_final → END
    - SE "requer_refinamento" → no_roteador (iteração 2, com <<critica>> inclusa)

Iteração 2 e 3: mesmo fluxo.

Iteração 3 (máxima):
  - Se ainda "requer_refinamento": seta deve_reprocessar = True, força "aprovado"
  - relatorio_final inclui aviso de que o migration_agent deve refazer a migração
```

---

## Exemplo de Uso

### Cenário: migração de autenticação MD5 → bcrypt

```bash
curl -X POST http://127.0.0.1:8000/review \
  -H "Content-Type: application/json" \
  -d '{
    "codigo_original": "import hashlib\n\ndef autenticar(senha, hash_bd):\n    return hashlib.md5(senha.encode()).hexdigest() == hash_bd\n",
    "codigo_migrado":  "import bcrypt\n\ndef autenticar(senha, hash_bd):\n    return bcrypt.checkpw(senha.encode(), hash_bd)\n"
  }'
```

**Trecho do relatório gerado:**

```markdown
## Achados de Semântica
- `autenticar` agora espera `hash_bd` como `bytes` em vez de `str`,
  quebrando compatibilidade com hashes existentes no banco (linha 4 do migrado).
  Trigger: primeira autenticação de usuário após o deploy.

## Achados de Segurança
- [AUTH-LOG] Migração de MD5 para bcrypt é correta, mas hashes legados
  precisam ser re-hasheados na próxima autenticação.
  Trigger: usuários com contas criadas antes da migração.

## Veredito Final
APROVADO COM RESSALVAS — implementar migração transparente de hashes (hash-on-login).
```

---

## Notas Técnicas

- O agente **não corrige código** — apenas reporta e orienta o desenvolvedor.
- O `no_parser` usa `git diff --no-index` para diff **determinístico** entre arquivos arbitrários (sem repositório git ativo). Fallback para diff baseado em LLM se `git` não estiver no PATH.
- O `no_lint` executa Ruff via `subprocess` — determinístico, sem tokens de LLM para detecção. O LLM é usado apenas para **interpretar** as regressões (issues novos que não existiam no original).
- Os prompts são carregados em memória **uma única vez** na inicialização — sem overhead de I/O por requisição.
- O número máximo de iterações é configurável pela constante `_MAX_ITERACOES` em `review-agent.py`.
- `deve_reprocessar = True` é o sinal para o `migration_agent` refazer a migração quando o review_agent esgota iterações sem aprovação.
