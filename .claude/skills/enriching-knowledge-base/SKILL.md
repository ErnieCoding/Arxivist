---
name: enriching-knowledge-base
description: Researches a company or topic from the web using WebSearch and WebFetch, structures the gathered intelligence, and persists it in the ArangoDB knowledge graph using the mcp__kb__* tools. Use this when the user wants to enrich the knowledge base with external data about a company, HR strategy, competitors, vacancies, or any other real-world topic before a meeting or analysis session.
---

# Enriching the Knowledge Base

## When to activate
- "Обогати базу о компании X", "найди информацию о компании до встречи"
- "Добавь в базу данные о конкурентах / HR-стратегии / рынке"
- After `mcp__kb__query_knowledge_base` returns sparse results and the user wants fresh data

## Full workflow

```
Step 1: Query existing KB
  → mcp__kb__query_knowledge_base(database_name="companies", question="<company name> <focus>")
  If sufficient data exists → stop and use it
  If sparse → continue to Step 2

Step 2: Web research (parallel searches)
  → WebSearch("<Company Name> official site description industry")
  → WebSearch("<Company Name> news 2024 2025")
  → WebSearch("<Company Name> <focus area>")   [HR-стратегия / конкуренты / финансы / etc.]
  → WebFetch on the most relevant 4-6 URLs (homepage, About, press, LinkedIn)

Step 3: HH data (if focus = vacancies/HR)
  → mcp__hh__search_hh_employers(text="<company name>")
  → mcp__hh__get_hh_employer_details(employer_id=<id>, include_vacancies=true)

Step 4: Structure the gathered data into ONE JSON object
  The KB ingests structured JSON (not prose). Build a single document with
  consistent field names (see ingesting-to-knowledge-base for the full schema):
    {
      "company": "<Name>",
      "aliases": ["<English name>"],
      "industry": "...",
      "founded": <year>,
      "headquarters": "...",
      "website": "...",
      "employees": "...",
      "description": "2-4 sentence overview",
      "focus_area": "<the user's area of interest>",
      "hr_and_people": "...",
      "recent_news": [{"date": "YYYY-MM", "event": "..."}],
      "vacancies": {"count": <n>, "key_roles": ["..."]},
      "competitors": ["..."],
      "financials": "...",
      "sources": ["<URL>", "..."],
      "gathered_at": "<today YYYY-MM-DD>"
    }
  Only include fields you actually found — do not fabricate.

Step 5: Ingest (auto-creates the database on first use)
  → mcp__kb__add_document_to_kb(
      database_name="companies",
      document={ ...the JSON object above... },
      filename="<company-slug>.json"
    )
  The tool creates 'companies' if missing, else appends, and waits for processing.

Step 6: Report to user (clean, user-facing)
  Briefly confirm that information about the company was gathered and saved,
  then present the key findings and list the sources. Do NOT expose database
  names, filenames, JSON, tool names, or step-by-step mechanics.
```

## Notes
- Always search in both Russian and English (translate company name if needed)
- Disclose sources to the user
- Do not invent data not found in search results
- Limit to 5-8 web pages to stay within time budget
- One JSON document per company; keep field names stable so the graph schema is consistent
