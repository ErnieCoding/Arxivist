---
name: ingesting-to-knowledge-base
description: Adds, queries, and manages data in the ArangoDB-backed knowledge graph. Use this skill for context on when and how to use the native mcp__kb__* MCP tools for knowledge base operations.
---

# Ingesting to Knowledge Base

> **Note:** Direct KB operations now use native MCP tools — no call_api needed.
> The tools handle auth, retries, and error reporting automatically.

## Available MCP tools

| Tool | When to use |
|---|---|
| `mcp__kb__list_kb_databases` | Discover available databases |
| `mcp__kb__query_knowledge_base` | **Ask first** — check what's already known |
| `mcp__kb__create_kb_database` | Create a database that doesn't exist yet |
| `mcp__kb__expand_knowledge_base` | Add new text chunks to a database |
| `mcp__kb__get_kb_task_status` | Poll status of async create/expand |

## Default database: `"companies"`
Used for all company intelligence data. Create it if it doesn't exist.

## Workflow for ingesting company data

1. `mcp__kb__list_kb_databases` → verify `"companies"` exists (create if not)
2. `mcp__kb__query_knowledge_base(database_name="companies", question="<company name>")` → check existing data
3. `mcp__kb__expand_knowledge_base(database_name="companies", texts=[...], metadata=[...])` → add new chunks

## Chunk guidelines
- 300–800 tokens per chunk
- Each chunk = one self-contained factual paragraph
- Metadata per chunk: `{"type": "company_profile|hr_data|news|vacancy|financial", "source": "URL", "company": "Name", "date": "YYYY-MM-DD"}`

## Connection details
- Base URL: `http://neo.rndl.ru:5001`
- Auth: `X-API-Key` header (read from `KNOWLEDGE_BASE_API_KEY` env — handled by the MCP tool)
