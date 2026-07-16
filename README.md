# Arxivist

AI research assistant in a chat box. Search and download arXiv papers, summarize them, upload your own documents, and generate (or edit) self-contained HTML dashboards — all from a single conversational interface.

Powered by the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) and Flask. The agent extends itself: when you mention a new database or API, it writes its own SKILL.md file to learn how to talk to it.

> **Developer documentation** — full module map, request flows, protocols, and extension guide: [docs/DEVELOPER.md](docs/DEVELOPER.md)

---

## Features

- **arXiv search** — natural-language queries (any language) are translated and routed through the arXiv API with rate limiting and exponential backoff.
- **Paper summarization** — structured markdown summaries from downloaded abstracts.
- **File uploads** — attach PDF, DOCX, MD, or TXT files to chat messages. PDFs are parsed natively by Claude (figures, tables, layout).
- **Dashboard generation** — turn any document, pasted text, or downloaded paper into a published, single-file HTML dashboard with cards, timelines, stats, and Chart.js charts.
- **In-place dashboard editing** — say "make the cards rounded" or "add a section on risks" and the agent edits the live page without re-creating it.
- **Self-extending skills** — provide an API's base URL and auth, and the agent writes a new `SKILL.md` so it can call that API on demand.

---

## Requirements

- Python 3.11+
- Docker + Docker Compose (for the production-like setup)
- An Anthropic API key

---

## Setup

1. Clone the repo and `cd` into it.
2. Create a `.env` file at the repo root:

   ```env
   ANTHROPIC_API_KEY=sk-ant-...
   ```

3. (Optional) For local dev outside Docker, create a virtualenv and install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

---

## Running

### Local development

Runs the Flask dev server on `http://127.0.0.1:5000`:

```bash
python app.py
```

Open `http://127.0.0.1:5000/` in a browser.

### Production (Docker, behind nginx)

The Docker image runs gunicorn on `127.0.0.1:5050`. Intended to sit behind a host nginx that proxies `/arxivist/` to it.

```bash
docker compose up --build -d
docker compose logs -f arxivist
```

The `APP_BASE=/arxivist` environment variable (set in `docker-compose.yml`) is passed to the frontend so client-side fetch URLs are correct. Change it if you deploy at a different sub-path.

If you're putting nginx in front of it, make sure `client_max_body_size` is at least 25 MB so file uploads aren't rejected.

---

## Use cases

The chat box accepts any natural-language request. The agent matches your wording to one of its skills and runs the corresponding workflow. Examples:

### 1. Search and download papers

> "Find me 5 recent papers on diffusion models by LeCun."

The agent translates non-English queries to English, hits arXiv, downloads the PDFs to `downloads/`, and returns a structured summary of each one (title, authors, category, date, abstract overview). Downloaded files appear in the **Downloaded Papers** sidebar.

### 2. Summarize existing downloads

> "Summarize the papers I have."

The agent looks at what's in `downloads/` and produces fresh summaries from the metadata.

### 3. Upload a document and ask questions

Click the **+** button next to the message box (or drag a file onto the textarea) to attach PDF/DOCX/MD/TXT files. They are stored under `uploads/<file_id>/`.

> "What are the three biggest takeaways from this report?"

For PDFs, the agent uses Claude's native PDF reading (sees images, tables, and layout). For DOCX, the agent reads pre-extracted markdown text with tables preserved.

### 4. Generate a dashboard from a document

> "Build a dashboard from this PDF."

The agent:
1. Reads the source (upload, pasted text, or downloaded arXiv PDFs).
2. Plans the structure as JSON in its reasoning (overview / cards / timeline / metrics / charts / etc.).
3. Renders a single self-contained HTML file following [`design-system.md`](design-system.md) — palette via CSS variables, predictable class/ID names, anchor-marker comments.
4. Publishes it via the `create_dashboard` MCP tool, which mints a UUID and writes to `dashboards/<uuid>/index.html`.
5. Replies with a link like `[Open the dashboard](/arxivist/d/<uuid>)`.

The dashboard opens at its own URL and appears in the **Dashboards** sidebar (which lists every dashboard ever created, newest first). All visible text — titles, body copy, chart labels — is rendered in the source document's language.

You can also paste raw text into the chat box (no file needed) and ask for a dashboard from it.

### 5. Chain arXiv search → dashboard

> "Find three recent papers on retrieval-augmented generation and make a dashboard comparing them."

The agent runs `searching-arxiv` first, then `creating-dashboards` over the downloaded PDFs.

### 6. Edit a dashboard

Once a dashboard exists, the chat session "remembers" it (server-side, keyed by a `session_id` minted in your browser's `localStorage`).

> "Make the cards rounded."
> "Add a section on risks."
> "Remove the timeline and recolor the accent to green."

The agent reads the existing HTML, classifies the change (CSS / HTML / JS), and applies precise edits against the documented anchor markers. The URL stays the same — refresh to see changes.

### 7. Add a new database or API

> "We use Chroma at http://localhost:8000. Auth header is `X-API-Key`, env var `CHROMA_KEY`. Endpoint POST /api/v1/add takes {documents, embeddings}."

The agent uses its `creating-skills` skill to write a new `SKILL.md` describing this API, then immediately uses it to fulfill your request. The skill persists in the `arxivist_skills` Docker volume across rebuilds.

---

## Project layout

```
.
├── app.py                       # Flask app + agent loops + new routes
├── tools.py                     # arxiv MCP server (search/download/list)
├── tools/
│   ├── call_api.py              # generic HTTP MCP tool
│   └── dashboard_tools.py       # create_dashboard, extract_text
├── text_extract.py              # docx/md/txt extraction helpers
├── uploads_store.py             # upload save + meta.json
├── dashboards_store.py          # registry with fcntl.flock
├── design-system.md             # palette, naming, markers, HTML skeleton
├── templates/index.html         # frontend (chat + sidebars + uploads)
├── .claude/skills/              # skill instructions read by the agent
│   ├── searching-arxiv/
│   ├── summarizing-papers/
│   ├── creating-dashboards/
│   ├── editing-dashboards/
│   ├── reading-uploads/
│   └── creating-skills/
├── uploads/                     # user uploads (Docker named volume)
├── dashboards/                  # generated dashboards + registry.json
├── downloads/                   # downloaded arXiv PDFs (Docker volume)
├── Dockerfile
├── docker-compose.yml
├── gunicorn.conf.py
└── requirements.txt
```

---

## API endpoints

| Method | Path             | Purpose |
|--------|------------------|---------|
| GET    | `/`              | Serve the chat UI. |
| POST   | `/search`        | Structured paper search + summary pipeline. |
| POST   | `/chat`          | Open-ended agent loop. Accepts `{message, history, session_id, attached_file_ids}`. |
| POST   | `/upload`        | Multipart file upload. Returns `{files: [meta...], errors: [...]}`. |
| GET    | `/files`         | List downloaded arXiv PDFs. |
| GET    | `/d/<uuid>`      | Serve a generated dashboard's HTML. |
| GET    | `/dashboards`    | List all registered dashboards, newest first. |

---

## Configuration

| Env var              | Purpose                                                   | Default       |
|----------------------|-----------------------------------------------------------|---------------|
| `ANTHROPIC_API_KEY`  | Required — your Claude API key.                           | (none)        |
| `APP_BASE`           | Sub-path the app is served under (for reverse-proxy).     | `""` (root)   |

---

## Limitations

- Two gunicorn sync workers, one request per worker — no concurrent requests from the same worker. Sufficient for personal/small-team use; not multi-tenant SaaS.
- DOCX image extraction not implemented; figures inside `.docx` files aren't surfaced to the agent in v1. Convert to PDF for image fidelity.
- Legacy `.doc` is rejected — convert to `.docx`, `.pdf`, or `.txt`.
- Uploaded PDFs > 20 pages: the agent reads them in `pages` ranges, which costs more tokens and adds turns.
- `fcntl.flock` is POSIX-only (production container is Linux). Local Windows dev will degrade.
- No CSP currently on dashboard pages — dashboards are session-scoped UUIDs with unguessable paths, but the route is public.

---

## License

No license file is bundled. Use, fork, and adapt freely for your own research workflows.
