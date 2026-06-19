# Piloto — Preenchimento de código + ground truth do dataset

Documento do trabalho feito sobre
[`dataset/Request-Urllib-Codigo-Inteiro.xlsx`](Request-Urllib-Codigo-Inteiro.xlsx):
preenchimento das colunas de código completo (antes/depois da migração) e
criação da coluna **`ground truth`** com os problemas que o `review_agent`
deveria encontrar.

---

## 1. Objetivo

A planilha cataloga migrações reais de Python `urllib`/`urllib2` → `requests`,
extraídas de repositórios públicos do GitHub. Cada linha é uma mudança
(snippet) num arquivo de um commit. As metas deste piloto:

1. Preencher **`all_code_before`** — o arquivo **inteiro** antes da migração.
2. Preencher **`all_code_after`** — o arquivo **inteiro** depois da migração.
3. Criar **`ground truth`** — lista dos problemas (P0–P3) que a migração
   introduziu, no formato dos achados do agente, para servir de referência do
   que o `review_agent` deve detectar.

---

## 2. Estrutura da planilha

Aba única `Request-Urllib`, 200 linhas de dados (linhas 2–201), 81 repos
distintos. Colunas originais:

| Col | Nome | Conteúdo |
|-----|------|----------|
| A | `repo_name` | `owner/repo` do GitHub |
| B | `commit_code` | SHA do **commit da migração** |
| C | `file_name` | caminho do arquivo no repo |
| D | `type` | classificação da mudança (ex.: `complex`) |
| E | `legacy_lib` | `urllib` |
| F | `target_lib` | `requests` |
| G | `code_before` | **trecho** alterado, antes |
| H | `code_after` | **trecho** alterado, depois |
| I | `all_code_after` | arquivo inteiro depois (parcialmente preenchido) |
| J | `all_code_before` | arquivo inteiro antes (quase vazio) |
| **K** | **`ground truth`** | **NOVA — criada neste piloto** |

Estado inicial: `all_code_after` tinha 109/200 preenchidos; `all_code_before`
tinha apenas 1/200.

---

## 3. Semântica do commit (descoberta e validada)

`commit_code` é o **commit que aplicou a migração**. Logo:

- **`all_code_after`** = arquivo (`file_name`) **no** `commit_code`.
- **`all_code_before`** = arquivo **no commit pai** (`parents[0]` do `commit_code`).

Validado com o caso já preenchido `5agado/conversation-analyzer`
(commit `8989fba`, mensagem *"Modified scraper to use requests lib"*,
pai `6fc93d68`).

### Como o código foi obtido

1. SHA do pai: API do GitHub `GET /repos/{repo}/commits/{sha}` → `parents[0].sha`.
2. Conteúdo: `https://raw.githubusercontent.com/{repo}/{sha}/{file_name}`
   (after) e o mesmo com o SHA do pai (before).
3. Diff analisado com `git diff --no-index before after` (mesma técnica
   determinística que o `review_agent` usa no `no_parser`).

---

## 4. Decisões acordadas (escopo do piloto)

| Pergunta | Decisão |
|----------|---------|
| Formato do ground truth | **Lista P0–P3 em texto**, no formato dos achados (`- [PREFIX][Px] \`símbolo\` — problema. Trigger: …`) |
| Como gerar | **Análise por IA do diff** (best-effort) — **não** é gold verificado por humano |
| Escopo | **Piloto de 5–10 linhas** end-to-end para validar o formato antes de escalar |

---

## 5. Bloqueios encontrados

1. **Rate limit da API do GitHub = 60 req/hora** (sem `gh` instalado e sem
   token). Suficiente para o piloto; **inviável** para as 200 linhas num passe.
2. **Repos mortos** — ex.: `18F/cg-compliance` (row 2) retorna **HTTP 404**
   (deletado/renomeado). Essas linhas não podem ser preenchidas pelo GitHub.

---

## 6. O que foi preenchido

8 linhas processadas (rows 3–10). Rows 6/7/8 apontam para o mesmo arquivo
(`bitmex.py`) e rows 9/10 para o mesmo (`plexmediaserver.py`) — são mudanças em
trechos diferentes do mesmo commit, então `all_code_*` e `ground truth` são
idênticos por arquivo.

| row | repo / arquivo | before | after | ground truth (resumo) |
|-----|----------------|:------:|:-----:|------------------------|
| 2 | `18F/cg-compliance` | — | — | **404** — repo indisponível, não avaliado |
| 3 | `conversation-analyzer` / conversationScraper.py | ✅ | ✅ | P1: `executeRequest` sem `raise_for_status()` |
| 4 | `taskbutler` / todoist-progressbar.py | ✅ | ✅ | P1: `api.commit()` comentado; P3 timeout |
| 5 | `conductor_client` / api_client.py | ✅ | ✅ | P2 Content-Type×data; P3 `auth`/`get_token` mortos |
| 6/7/8 | `market-maker` / bitmex.py | ✅ | ✅ | P1 assinatura HMAC sem query; P1 header `accessToken`; P2 KeyError 404 |
| 9/10 | `autosub-bootstrapbill` / plexmediaserver.py | ✅ | ✅ | **P0** `ET.parse(Response)`; P2 sem `raise_for_status` |

---

## 7. Ground truth detalhado (por arquivo)

### row 3 — `conversation-analyzer` / `conversationScraper.py`
- **[COMPAT][P1] `executeRequest`** — `requests.post()` sem `raise_for_status()`;
  `urllib.request.urlopen` levantava `HTTPError` em 4xx/5xx, agora o status de
  erro é ignorado. **Trigger:** Facebook responde 404/500 → `response.text[9:]`
  trata o corpo de erro como sucesso.
- *Pré-existentes (não contar como P0/P1):* slice mágico `response.text[9:]`
  (prefixo anti-JSON-hijacking do FB); `json.loads` de `msgsData` no chamador.
  *Correto:* remoção do `gzip.GzipFile` manual — requests auto-descomprime.

### row 4 — `taskbutler` / `todoist-progressbar.py`
- **[COMPAT][P1] bloco `__main__` (sync)** — `api.commit()` foi **comentado**
  (`#api.commit()`); alterações em tarefas deixam de ser sincronizadas.
  **Trigger:** executar após modificar itens → "Sync done" é impresso mas nada
  persiste no Todoist.
- **[INFO][P3] update-check** — `requests.get(config.update_url)` sem `timeout=`
  (original também não tinha; não-regressão).
- *Correto:* `raise_for_status()` adicionado; shadowing do módulo `json`
  corrigido para `release_info_json`; ordem de `except` específica→geral ok.

### row 5 — `conductor_client` / `api_client.py`
- **[CONTRACT][P2] `make_request`** — `Content-Type: application/json` default
  enviado junto com `data=data`; se o chamador passar dict em `data`, requests
  reencoda como form-urlencoded, divergindo do header. **Trigger:** chamada com
  `data=dict`.
- **[TYPING-DRY][P3] `make_request`** — variável `auth = CONFIG['conductor_token']`
  criada e nunca usada (auth real é via `HTTPBasicAuth` inline).
- **[TYPING-DRY][P3] `ApiClient`** — `get_token()` ficou sem chamadores após
  remover `authorize_urllib()` (dead code).
- *Pré-existente (não contar):* `import urlparse`/`urlparse.urljoin` não migrado
  (código py2).

### rows 6/7/8 — `market-maker` / `bitmex.py`
- **[CONTRACT][P1] `_curl_bitmex`** — a assinatura HMAC é calculada sobre `url`
  **sem** a query string (a concatenação `url += '?' + urlencode(query)` foi
  removida), mas a requisição é enviada com `params=query`. A assinatura deixa
  de cobrir os parâmetros. **Trigger:** GET autenticado com query → servidor
  recomputa a assinatura com a query e rejeita (401 api-signature inválida).
- **[CONTRACT][P1] `connect`/`_curl_bitmex`** — header de auth por token
  renomeado de `accessToken` (original) para `access-token`. **Trigger:** login
  email/senha/otp (sem apiKey) → servidor espera `accessToken` → 401.
- **[COMPAT][P2] `_curl_bitmex`** — branch 404 do DELETE acessa
  `postdict['orderID']` assumindo `postdict` não-None. **Trigger:** DELETE sem
  postdict → KeyError (pré-existente, mantido).
- *Correto:* `raise_for_status()`, `response.json()`, `requests.Session` com
  user-agent, retry em Timeout.

### rows 9/10 — `autosub-bootstrapbill` / `plexmediaserver.py`
- **[CONTRACT][P0] `_update_library`** — `ET.parse(requests.get(url))` passa um
  objeto `requests.Response` ao `ET.parse`, que espera caminho de arquivo ou
  stream com `.read()`; `urllib.urlopen` retornava um file-like. **Trigger:**
  ramo sem token (`plexservertoken` ausente) → `ET.parse(Response)` levanta
  `TypeError`/`ParseError`, **não** capturado pelo `except IOError` → propaga.
  *Correção:* `ET.fromstring(requests.get(url).text)`.
- **[COMPAT][P2] `_update_library`** — nenhuma chamada `requests.*` usa
  `raise_for_status()`; `urllib2.urlopen` levantava em 4xx/5xx. **Trigger:**
  plex.tv responde 401/500 no sign_in → `response.text` é parseado como XML de
  sucesso (token vazio / erro silencioso).
- **[INFO][P3] `_update_library`** — refresh `requests.get(...)` fire-and-forget
  sem checagem de status (original idem; menor).

---

## 8. Avisos / limitações

- **O ground truth é gerado por IA, não verificado por humano.** Trate como
  rascunho de gold; a equipe deve revisar antes de usar em avaliação oficial.
- O ground truth distingue **regressões da migração** (o que o agente deve
  achar) de **problemas pré-existentes** (o que o agente **não** deve contar
  como P0/P1) — mesma filosofia do `review_agent/eval/gold/v1.json`.
- Linhas com o mesmo arquivo/commit (6/7/8 e 9/10) recebem código e ground
  truth idênticos por serem visões snippet-level do mesmo diff.

---

## 9. Como escalar para as 200 linhas

1. **Resolver o rate limit:** instalar/autenticar `gh` **ou** exportar
   `GITHUB_TOKEN` (60 → 5000 req/h). Sem isso, só ~20 linhas/hora.
2. **Pular repos 404** automaticamente (marcar na coluna como indisponível).
3. Reaproveitar o fluxo: API para o SHA do pai → raw para before/after →
   `git diff --no-index` → análise → escrita via `openpyxl`.
4. Reaproveitar `all_code_after` já preenchido (109 linhas) quando existir, em
   vez de rebaixar do GitHub.
5. **Validação cruzada (opcional):** rodar o próprio `review_agent` nesses pares
   e comparar com o ground truth — útil para medir recall, mas **não** para
   gerar o gold (seria circular).

---

## 10. Dependências usadas

- `openpyxl` (instalado neste piloto) para ler/escrever o `.xlsx`.
- `git`, `curl`/`urllib` e a API pública do GitHub para obter os códigos.
