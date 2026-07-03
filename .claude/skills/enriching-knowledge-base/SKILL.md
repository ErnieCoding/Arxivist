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

Step 4: Structure the gathered data
  Write self-contained factual paragraphs. One topic per paragraph (300-800 tokens):
    • Company profile (industry, size, founding, HQ, products/services)
    • HR & people (culture, hiring approach, key executives)
    • Recent news (list of dated events)
    • Vacancies (count, key roles, salary ranges, required skills)
    • Competitors (names + 1-line context)
    • Financials (revenue, funding — only public data)

Step 5: Ensure database exists
  → mcp__kb__list_kb_databases()
  If "companies" not in list:
    → mcp__kb__create_kb_database(database_name="companies")

Step 6: Ingest chunks
  → mcp__kb__expand_knowledge_base(
      database_name="companies",
      texts=["<chunk1>", "<chunk2>", ...],
      metadata=[
        {"type": "company_profile", "source": "<URL>", "company": "<Name>", "date": "<YYYY-MM-DD>"},
        ...
      ]
    )
  If response is 202 with task_id:
    → mcp__kb__get_kb_task_status(task_id=<id>)  [poll until done]

Step 7: Report to user
  "Добавил X чанков о компании Y в базу знаний 'companies'. Источники: ..."
```

## Notes
- Always search in both Russian and English (translate company name if needed)
- Disclose sources to the user
- Do not invent data not found in search results
- Limit to 5-8 web pages to stay within time budget
- Each chunk should be standalone — the KB stores them as independent graph nodes
