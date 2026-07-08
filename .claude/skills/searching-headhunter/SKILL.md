---
name: searching-headhunter
description: Searches HeadHunter (hh.ru) for vacancies, employers, HR data, skills, and salary stats. Use this skill for context on when and how to use the native mcp__hh__* MCP tools.
---

# Searching HeadHunter

> **Note:** HH API calls now use native MCP tools — no call_api needed.
> Auth (HH_ACCESS_TOKEN if set), User-Agent, and retry on 429 are handled automatically.

## Available MCP tools

| Tool | When to use |
|---|---|
| `mcp__hh__search_hh_vacancies` | Find job postings by keywords, region, experience, salary |
| `mcp__hh__get_hh_vacancy` | Full details for a specific vacancy (description, key skills) |
| `mcp__hh__search_hh_employers` | Find companies by name |
| `mcp__hh__get_hh_employer_details` | Company profile + open vacancies count |
| `mcp__hh__get_hh_reference` | Region codes, professional roles, skill autocomplete, dictionaries |

## Common region codes
`1` = Москва, `2` = Санкт-Петербург, `113` = Россия (вся страна).
Use `mcp__hh__get_hh_reference(type="areas", filter="<city>")` for other cities.

## Workflows

**"Найди вакансии [роль] в [город]":**
1. If city code unknown: `get_hh_reference(type="areas", filter="<город>")`
2. `search_hh_vacancies(text="<роль>", area=<code>, per_page=20)`
3. For full details on any vacancy: `get_hh_vacancy(vacancy_id="<id>")`

**"Информация о компании X на HH":**
1. `search_hh_employers(text="<Name>")` → get employer_id
2. `get_hh_employer_details(employer_id="<id>", include_vacancies=true)`

**"Какие навыки востребованы в [домен]":**
1. `search_hh_vacancies(text="<домен>", per_page=50)` × multiple pages
2. Collect `key_skills` from each vacancy, count frequencies

**"Зарплата для [роль]":**
1. `search_hh_vacancies(text="<роль>", area=113, per_page=100)` → extract salary ranges
2. Compute median/range from results

## Auth status (HH tightened their API — read this)
The mcp__hh__* tools attach a token automatically; you never manage auth. But the token must exist:

| Endpoint | Needs token? |
|---|---|
| `get_hh_reference` areas / professional_roles / dictionaries | No (public) |
| `search_hh_vacancies`, `get_hh_vacancy` | **Yes** — HH returns 403 without one |
| `search_hh_employers`, `get_hh_employer_details` | **Yes** — 403 without one |
| resume search | Yes + paid employer CV-subscription |

- **Application token (covers vacancy/employer search):** set `HH_CLIENT_ID` + `HH_CLIENT_SECRET` in .env.
  The tools then mint a `client_credentials` token automatically — no user login needed.
- **User token (for resumes):** operator visits `/hh/authorize` once; token stored + auto-refreshed.
- If a tool returns a 403 message, tell the user to configure `HH_CLIENT_ID`/`HH_CLIENT_SECRET`.
