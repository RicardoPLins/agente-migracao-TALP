# CodeReviewAgent

Agente de revisão de migrações de código construído com **LangGraph**, **Gemini 2.5 Flash** (nós pesados) e **Groq llama-3.1-8b-instant** (nós leves).

O agente analisa dois snapshots de código (original e migrado), gera um diff determinístico via `git diff`, aciona especialistas em paralelo e entrega um relatório consolidado em Markdown — **sem corrigir o código autonomamente**.

---

## Arquitetura

```
Entrada (original + migrado)
  │
  ▼
[no_parser]        → diff via git diff --no-index + extração estrutural (Gemini)
  │                  Produz: raw_diff (unified diff) + diff_estruturado (JSON)
  ▼
[no_classificador] → decide quais especialistas acionar com base no diff (Groq 8B)
  │                  Produz: agentes_acionados ⊆ {"semantica","seguranca","lint"}
  ▼
[no_roteador]      → fan-out paralelo via LangGraph Send()
  ├──► [no_semantico]  → equivalência funcional, contratos, null-safety (Gemini)
  ├──► [no_seguranca]  → autenticação, inputs, superfície de ataque (Gemini)
  └──► [no_lint]       → Ruff determinístico; LLM interpreta regressões (Groq 8B)
            │
            ▼
       [no_critico]    → meta-revisor com saída antecipada por severidade (Gemini)
            │
            ├─ todos achados são [P2]/[P3] → aprovado imediato (sem LLM)
            ├─ "aprovado" [P0]/[P1] ou iteração = 3
            │        ▼
            │  [relatorio_final]  → Markdown consolidado → END (Groq 8B)
            │
            └─ "requer_refinamento" [P0]/[P1]
                     ▼
               [no_roteador]  (nova rodada — contexto compacto nas iter 2+)
```

### Nós, responsabilidades e modelos

| Nó | Função | Modelo |
|---|---|---|
| `no_parser` | Gera `raw_diff` via `git diff --no-index`; extrai diff estruturado (funções/classes/deps alteradas) | Gemini 2.5 Flash |
| `no_classificador` | Roteia para especialistas com base em padrões do diff | Groq llama-3.1-8b-instant |
| `no_roteador` | Despacha tarefas em paralelo via `Send()`; reseta achados a cada iteração | — (sem LLM) |
| `no_semantico` | Equivalência de comportamento, contratos de API, semântica de null, compatibilidade retroativa | Gemini 2.5 Flash |
| `no_seguranca` | Autenticação/logging, validação de inputs, superfície de ataque, dependências com CVEs | Gemini 2.5 Flash |
| `no_lint` | Executa Ruff via `subprocess`; filtra issues pré-existentes; LLM interpreta regressões novas | Groq llama-3.1-8b-instant |
| `no_critico` | Meta-avaliador de qualidade; saída antecipada se não há [P0]/[P1] | Gemini 2.5 Flash |
| `relatorio_final` | Consolida todos os achados em Markdown com veredito final | Groq llama-3.1-8b-instant |

---

## Pré-requisitos

- Python **3.10+**
- Conta e chave de API na [Groq](https://console.groq.com/) — nós leves
- Conta e chave de API no [Google AI Studio](https://aistudio.google.com/apikey) — nós pesados
- [Ruff](https://docs.astral.sh/ruff/) acessível no `PATH`
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

Crie um arquivo `.env` na raiz do repositório (`agente-migracao-TALP/.env`) ou dentro de `review_agent/`:

```env
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
GOOGLE_API_KEY=AIzaxxxxxxxxxxxxxxxxxxxxxxxxxx
```

| Variável | Descrição | Obrigatória |
|---|---|---|
| `GROQ_API_KEY` | Chave da Groq — nós leves (classificador, lint, relatório) | Sim |
| `GOOGLE_API_KEY` | Chave do Google AI — nós pesados (parser, semântico, segurança, crítico) | Sim |
| `LANGSMITH_API_KEY` | Habilita tracing via LangSmith | Não |
| `LANGSMITH_PROJECT` | Nome do projeto no LangSmith | Não |

---

## Modos de uso

### Modo script de teste direto

```powershell
# Na raiz do repositório, com as chaves no .env:
.\review_agent\.venv\Scripts\python.exe .\review_agent\testReviewAgent.py
```

Usa os arquivos em `review_agent/test1/` como entrada.

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

```python
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # carrega GROQ_API_KEY e GOOGLE_API_KEY do .env

sys.path.insert(0, "review_agent")
import importlib.util

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
| `achados_semantica` | string[] | Problemas de equivalência semântica com severidade [P0]-[P3] |
| `achados_seguranca` | string[] | Riscos de segurança com severidade [P0]-[P3] |
| `achados_lint` | string[] | Regressões de lint/style com severidade [P0]/[P2]/[P3] |
| `iteracoes` | int | Rodadas do Reflection Loop executadas (máx 3) |
| `deve_reprocessar` | bool | `true` quando no_critico sinaliza que a migração deve ser refeita |
| `relatorio_final` | string | Relatório Markdown consolidado com veredito final |

---

## Estrutura do Projeto

```
review_agent/
├── prompts/
│   ├── parser.json                   # Extração de diff estruturado + uso de raw_diff
│   ├── classificador.json            # Roteamento granular com categorias de skills
│   ├── agente_semantica.json         # Equivalência funcional + escala P0-P3
│   ├── agente_seguranca.json         # Segurança + escala P0-P3
│   ├── agente_lint_config.json       # Inferência de config Ruff a partir do código original
│   ├── agente_lint_interpretacao.json# Interpretação de regressões Ruff + mapeamento P0/P2/P3
│   ├── no_critico.json               # Meta-revisor ciente de P0-P3 (Reflection Loop)
│   └── relatorio_final.json          # Consolidação final
├── test1/
│   ├── original.py                   # Código urllib de exemplo (ConversationScraper)
│   ├── migrado.py                    # Versão migrada para requests
│   └── ...                          # Outros artefatos de teste
├── .venv/                            # Ambiente virtual Python
├── review-agent.py                   # Orquestrador LangGraph + API FastAPI
├── testReviewAgent.py                # Script de teste standalone
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
   - Calibração de confiança por achado, com exemplos de formato correto
   - Campo `Trigger:` obrigatório — cada achado precisa descrever *quando* se manifesta
   - Anti-especulação — proibição de reportar achados sem evidência direta no diff
   - Referência de linha obrigatória: `(linha N do migrado)`

### Escala de severidade padronizada P0-P3

Todos os agentes especialistas usam a mesma escala, tornando a detecção pelo `no_critico` confiável:

| Label | Px | Definição |
|---|---|---|
| `[CONTRATO]`, `[NULL]`, `[COMPAT]` | `[P0]` ou `[P1]` | Quebra funcional ou mudança comportamental silenciosa |
| `[AUTH-LOG]`, `[INPUT-SEC]`, `[SURFACE]` | `[P0]` ou `[P1]` | Vulnerabilidade de segurança direta ou com precondições |
| `[BLOCKER]` | `[P0]` | Issue Ruff que quebra em runtime |
| `[AVISO]`, `[TYPING-DRY]` | `[P2]` | Qualidade/manutenibilidade |
| `[COSMÉTICO]`, `[NAMING]` | `[P3]` | Sugestão; não bloqueia o merge |

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

O placeholder `<<critica>>` é preenchido pelo `no_critico` nas iterações de refinamento; na primeira rodada é string vazia.

---

## Comportamento do Reflection Loop

```
Iteração 1:
  no_roteador → [semantico + seguranca + lint em paralelo] → no_critico

  no_critico verifica severidade:
    - SE nenhum achado tem [P0] ou [P1] → aprovado imediato (sem chamar LLM)
    - SE há [P0]/[P1] → LLM avalia qualidade:
        · "aprovado"            → relatorio_final → END
        · "requer_refinamento"  → no_roteador (iteração 2)

Iteração 2+ (refinamento):
  · no_semantico e no_seguranca recebem contexto compacto
    (não reenviam os arquivos completos — economiza ~60% dos tokens)
  · no_lint reutiliza config Ruff e issues cacheados da iteração 1
    (Ruff é determinístico; o código não mudou)
  · mesma verificação de severidade no no_critico

Iteração 3 (máxima):
  - Se LLM ainda rejeita: seta deve_reprocessar = True, força "aprovado"
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
- [CONTRATO][P0] `autenticar` (linha 4) — agora espera `hash_bd` como `bytes`
  em vez de `str`, quebrando compatibilidade com hashes existentes no banco.
  Trigger: primeira autenticação de usuário após o deploy.

## Achados de Segurança
- [AUTH-LOG][P1] Migração de MD5 para bcrypt é correta, mas hashes legados
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
- O `no_critico` implementa **saída antecipada por severidade**: se nenhum achado contém `[P0]` ou `[P1]`, aprova imediatamente sem chamar o LLM — achados cosméticos não justificam iterações extras.
- Nas **iterações 2+**, `no_semantico` e `no_seguranca` recebem apenas o `raw_diff` + crítica, sem reenviar os arquivos completos (economia de ~60% de tokens por iteração de refinamento).
- O `no_lint` **cacheia** a config Ruff inferida e os issues novos da iteração 1, reutilizando-os nas iterações seguintes (o Ruff é determinístico; o código não muda entre iterações).
- Todos os nós usam `_invoke_com_retry` com backoff exponencial (30s → 60s → 120s) como rede de segurança para rate limits.
- Os prompts são carregados em memória **uma única vez** na inicialização — sem overhead de I/O por requisição.
- O número máximo de iterações é configurável pela constante `_MAX_ITERACOES` em `review-agent.py`.
- `deve_reprocessar = True` é o sinal para o `migration_agent` refazer a migração quando o review_agent esgota iterações sem aprovação.
