# Arxivist

An AI assistant in a chat box that has grown well beyond arXiv: research papers, document dashboards, a company knowledge graph, and a full HeadHunter recruiting workflow — candidate search driven by plain conversation or an attached vacancy description.

Powered by the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) (agent loop + in-process MCP tool servers) and Flask. The agent extends itself at runtime: mention a new database or API, and it writes its own `SKILL.md` to learn how to talk to it.

> **Developer documentation** — full module map, request flows, protocols, and extension guide: [docs/DEVELOPER.md](docs/DEVELOPER.md)

---

## Features

### Research
- **arXiv search & download** — natural-language queries in any language (auto-translated), rate-limited arXiv API client with exponential backoff.
- **Paper summarization** — deterministic structured summaries produced by a direct API call (not the agent), so the format never drifts.

### Documents & dashboards
- **File uploads** — attach PDF / DOCX / MD / TXT (≤ 25 MB) to any chat message. PDFs are parsed natively by Claude (figures, tables, layout); DOCX tables are extracted as markdown.
- **Dashboard generation** — turn any document or pasted text into a published, single-file HTML dashboard at a sharable URL.
- **In-place dashboard editing** — "make the cards rounded", "add a risks section" — the agent edits the live page via anchored markers, same URL.

### Knowledge base (ArangoDB graph)
- **Query** — ask about a company/topic; the agent checks the knowledge base first and answers from it.
- **Enrich** — web research (built-in WebSearch/WebFetch) → structured JSON → ingested into the graph. The `companies` database holds company intelligence.

### HeadHunter (hh.ru) recruiting
- **Vacancy / employer search** — positions, salaries, hiring trends, company profiles. Works out of the box with app credentials (token minted and refreshed automatically).
- **Candidate / resume search** — by conversational criteria **or from an attached vacancy description**: the agent reads the document, distills role / region / experience / salary into a query, and returns a ranked list with resume links.
- **Precise filters, used only when asked** — salary range, schedule (remote/…), employment type, education level, age, relocation, search activity, resume freshness, sort order. Soft criteria stay in the text query so filters never silently shrink the pool.
- **One-time OAuth with auto-continue** — when resume search needs a sign-in, the agent posts a login link; after the user authorizes on hh.ru the callback tab closes itself and **the chat resumes the search automatically** (no manual "I'm back" message).
- **Save candidates to the knowledge base** — the user picks candidates from the list ("save 1, 3 and 5"); each is stored in the `candidates` database as one document: structured profile + resume link. Nothing is saved without an explicit pick (resumes are personal data).

### UX & infrastructure
- **Live status streaming** — `/chat` streams SSE status phrases ("Ищу резюме…", "Собираю дашборд…") in the user's language while the agent works; the final answer contains results only, never internal mechanics.
- **Sanitized rendering** — agent markdown goes through DOMPurify; external data (vacancy titles, resume fields) cannot inject HTML.
- **Direct / Proxy toggle** — a header switch routes model traffic either straight to the Anthropic API or through an upstream proxy (persisted across restarts).
- **Self-extending skills** — describe any REST API in chat and the agent writes a new skill for it, then calls it through a generic HTTP tool.

---

## Project structure

```
Arxivist/
├── app.py                  # Flask app: routes, /chat SSE loop, agent options, HH OAuth
├── arxiv_tools.py          # MCP server `arxiv` + per-request session state (_session)
├── tools/
│   ├── hh_tools.py         # MCP server `hh` — vacancies, employers, resumes (typed filters)
│   ├── hh_auth.py          # HH OAuth2: app/user tokens, refresh, CSRF state
│   ├── kb_tools.py         # MCP server `kb` — knowledge-graph query/ingest (JSON-only)
│   ├── dashboard_tools.py  # MCP server `dashboard` — publish HTML, extract text
│   └── call_api.py         # MCP server `api` — generic HTTP tool (no API specifics, by design)
├── bridge/bridge.py        # Anthropic-API bridge: direct ↔ upstream-proxy modes, /mode switch
├── dashboards_store.py     # dashboard registry (flock-protected, 2 workers)
├── uploads_store.py        # upload validation + text extraction + meta.json
├── text_extract.py         # DOCX (table-aware) / MD / TXT extraction; PDFs go to Claude natively
├── templates/index.html    # single-file frontend: SSE reader, OAuth auto-continue, mode toggle
├── .claude/skills/         # agent skills (orchestration + self-extension), seeded into a volume
├── docs/DEVELOPER.md       # developer guide (modules, flows, protocols, extension)
├── design-system.md        # dashboard design system the agent must follow
├── Dockerfile / docker-compose.yml / entrypoint.sh / gunicorn.conf.py
└── config/                 # runtime state (gitignored): HH tokens, OAuth state, bridge mode
```

Note: `arxiv_tools.py` lives at the root (not in `tools/`) because it owns the module-level session state imported by `app.py`; the name also avoids a package/module collision with `tools/`.

---

## Requirements

- Python 3.11+
- Docker + Docker Compose (for the production-like setup)
- An Anthropic API key (or upstream-proxy credentials)
- Optional: HeadHunter app credentials (dev.hh.ru), knowledge-base API key

---

## Setup

1. Clone the repo and create `.env` at the root:

   ```env
   # Model access — direct mode
   ANTHROPIC_API_KEY=sk-ant-...
   ANTHROPIC_BASE_URL=http://127.0.0.1:9999      # always the local bridge

   # Model access — proxy mode (optional; enables the header toggle)
   PROXY_UPSTREAM_URL=https://.../proxy/anthropic
   PROXY_AUTH_TOKEN=...
   PROXY_STREAM_URL_TEMPLATE=https://.../proxy/stream/{task_id}

   # Deployed behind nginx at a sub-path
   APP_BASE=/arxivist

   # Knowledge base (ArangoDB graph)
   KNOWLEDGE_BASE_URL=http://neo.rndl.ru:5001
   KNOWLEDGE_BASE_API_KEY=...

   # HeadHunter (register an app at https://dev.hh.ru)
   HH_CLIENT_ID=...
   HH_CLIENT_SECRET=...
   # Must EXACTLY match a Redirect URI registered in the HH app.
   # For local testing:  http://localhost:5000/hh/callback
   # For production: replace with your public URL, e.g. https://<host>/arxivist/hh/callback
   HH_REDIRECT_URI=http://localhost:5000/hh/callback
   ```

2. Run:

   ```bash
   # Production-like (bridge + gunicorn in Docker)
   docker compose up --build -d
   docker compose logs -f arxivist

   # Local dev (Flask dev server on :5000; bridge must run separately if needed)
   python app.py
   ```

   The container binds to `127.0.0.1:5050` (for nginx) **and** `127.0.0.1:5000` (so the locally registered HH OAuth callback resolves).

### HeadHunter access levels

| Capability | Needs |
|---|---|
| Region/role/skill reference data | nothing |
| Vacancy & employer search | `HH_CLIENT_ID` + `HH_CLIENT_SECRET` (app token is minted and re-minted automatically) |
| Resume / candidate search | one-time user sign-in via the link the agent posts (token auto-refreshes, ~14-day cycle) + a paid CV-database subscription on the HH employer account |

The OAuth flow is CSRF-protected and fully automated end-to-end: link in chat → sign in on hh.ru → callback tab closes itself → the chat continues the search on its own.

---

## Usage examples

| You say | What happens |
|---|---|
| «Найди свежие статьи про diffusion models» | arXiv search → PDFs downloaded → structured summaries |
| *(attach report.pdf)* «Сделай дашборд по этому отчёту» | native PDF read → published dashboard link |
| «Сделай карточки круглее» | in-place edit of the same dashboard |
| «Что мы знаем о компании X?» | knowledge-base query first; offers web enrichment if empty |
| «Найди вакансии Python в Москве, какая вилка?» | HH vacancy search + salary stats |
| *(attach vacancy.docx)* «Подбери кандидатов под эту вакансию» | reads the doc → distills criteria → ranked resume list with links |
| «Только удалёнка и активно ищущие, до 400к» | re-search with `schedule=remote`, `job_search_status=active_search`, `salary_to=400000` |
| «Сохрани кандидатов 1, 3 и 5» | enriches each resume → stores profile + resume link in the `candidates` knowledge base |
| «Подключись к базе Bitrix24: вот endpoint и токен…» | the agent writes a new SKILL.md and starts calling that API |

---

## Deployment notes

- Host nginx: `location /arxivist/ { proxy_pass http://127.0.0.1:5050/; proxy_buffering off; client_max_body_size 25m; }` — `proxy_buffering off` is required for `/chat` SSE.
- `APP_BASE` must match the nginx sub-path; client JS builds URLs from it.
- Before production: change `HH_REDIRECT_URI` from localhost to the public callback URL (and register it in the HH app), or authorization will silently break.
- Concurrency model: 2 sync gunicorn workers, one request each — session state is module-level and **not thread-safe**; don't add threads/async workers without refactoring it (see [docs/DEVELOPER.md §10](docs/DEVELOPER.md)).
- Runtime secrets live in `config/` (gitignored): HH tokens, OAuth state, bridge mode.

---

## Documentation

- [docs/DEVELOPER.md](docs/DEVELOPER.md) — architecture, module map, request flows, HTTP API, MCP tools, extension guide, known pitfalls.
- `CLAUDE.md` — working notes for AI-assisted development in this repo.
- `design-system.md` — the dashboard design contract the agent follows.
