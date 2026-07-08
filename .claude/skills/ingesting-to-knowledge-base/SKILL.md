---
name: ingesting-to-knowledge-base
description: Adds, queries, and manages data in the ArangoDB-backed knowledge graph. Use this skill for context on when and how to use the native mcp__kb__* MCP tools for knowledge base operations.
---

# Ingesting to Knowledge Base

> Direct KB operations use native MCP tools — no call_api needed.
> Auth, base64 encoding, task polling, and create-vs-append routing are handled by the tools.

## Available MCP tools

| Tool | When to use |
|---|---|
| `mcp__kb__list_kb_databases` | Discover available databases |
| `mcp__kb__query_knowledge_base` | **Ask first** — check what's already known |
| `mcp__kb__add_document_to_kb` | Ingest a structured JSON document (auto-creates db) |
| `mcp__kb__get_kb_task_status` | Check a background task by id |

## Critical: the KB ingests STRUCTURED JSON, not raw prose

The graph is built by mapping JSON fields into nodes/edges. Passing a wall of
text will be rejected. Always build a JSON **object** and pass it as `document`.

`add_document_to_kb` handles the rest:
- If the database does not exist → uploads the file and creates the database.
- If it exists → appends the document.
- Polls until the graph finishes processing.

## Default database: `"companies"`

## Company document schema (keep field names consistent across documents)

```json
{
  "company": "Яндекс",
  "aliases": ["Yandex"],
  "industry": "Технологии / Интернет",
  "founded": 1997,
  "headquarters": "Москва, Россия",
  "website": "https://yandex.ru",
  "employees": "более 20000",
  "description": "2–4 предложения о том, чем занимается компания.",
  "focus_area": "HR-стратегия",
  "hr_and_people": "Подход к найму, культура, ключевые руководители.",
  "recent_news": [
    {"date": "2025-03", "event": "краткое описание"}
  ],
  "vacancies": {"count": 42, "key_roles": ["ML Engineer", "HR BP"]},
  "competitors": ["VK", "Сбер"],
  "financials": "выручка / раунды, если публично",
  "sources": ["https://…", "https://…"],
  "gathered_at": "2026-07-01"
}
```

## Workflow

1. `mcp__kb__query_knowledge_base(database_name="companies", question="<entity>")` — check existing data first.
2. Gather / structure new data into the JSON schema above.
3. `mcp__kb__add_document_to_kb(database_name="companies", document={...}, filename="<slug>.json")`.
4. Confirm to the user in plain language that the information about `<entity>` was saved. Do NOT expose database names, filenames, JSON, or tool mechanics.

## Connection details
- Base URL: `http://neo.rndl.ru:5001`
- Auth: `X-API-Key` header (from `KNOWLEDGE_BASE_API_KEY` env — handled by the MCP tool)
- Ingestion endpoints (handled by the tool): `/api/server/files/upload`, `/api/server/database/create-from-files`, `/api/server/database/add-file`, `/api/server/tasks/{id}`.
