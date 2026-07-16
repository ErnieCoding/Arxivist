---
name: searching-headhunter
description: Searches HeadHunter (hh.ru) for vacancies, employers, HR data, skills, and salary stats. Use this skill for context on when and how to use the native mcp__hh__* MCP tools.
---

# Searching HeadHunter

> **Note:** HH API calls now use native MCP tools — no call_api needed.
> Auth is fully automatic: an application token is minted from HH_CLIENT_ID/HH_CLIENT_SECRET
> (covers vacancy/employer search), a user token is obtained via /hh/authorize (covers resumes).
> User-Agent, token refresh, and retry on 429/403 are handled in code.

## Available MCP tools

| Tool | When to use |
|---|---|
| `mcp__hh__search_hh_vacancies` | Find job postings by keywords, region, experience, salary |
| `mcp__hh__get_hh_vacancy` | Full details for a specific vacancy (description, key skills) |
| `mcp__hh__search_hh_employers` | Find companies by name |
| `mcp__hh__get_hh_employer_details` | Company profile + open vacancies count |
| `mcp__hh__get_hh_reference` | Region codes, professional roles, skill autocomplete, dictionaries |
| `mcp__hh__search_hh_resumes` | Search candidates/resumes (needs user sign-in + paid CV access) |
| `mcp__hh__get_hh_resume` | Full details of one resume by id (enrich before saving) |

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

**"Найди кандидатов с критериями" (resume search + save):**
1. `search_hh_resumes(text="<роль/навыки>", area=<code>, experience=<level>)`.
2. Present the results to the user as a clean numbered list: role, region, experience, salary,
   and the **resume link** for each. Then ASK which candidates to save — never save automatically.
3. When the user picks candidates (e.g. "сохрани 1, 3 и 5"):
   - For each chosen candidate, take the resume URL and derive `resume_id` (last segment of hh.ru/resume/<id>).
   - Optionally `get_hh_resume(resume_id)` to enrich (skills, work history, education).
   - Save with `add_document_to_kb(database_name="candidates", document={...}, filename="candidate-<id>.json")`.
4. Confirm briefly which candidates were saved (no internal mechanics).

### Candidate document schema (database `candidates`)
Keep field names consistent. One document = candidate + their resume together:
```json
{
  "candidate": "<role/title, e.g. 'Python-разработчик'>",
  "resume_id": "<hh id>",
  "resume_url": "https://hh.ru/resume/<id>",
  "area": "<region>",
  "experience_years": <int>,
  "key_skills": ["..."],
  "specializations": ["..."],
  "salary": "<expected, if any>",
  "education": "<level / details>",
  "last_position": "<most recent role — company>",
  "source": "HeadHunter",
  "saved_at": "<YYYY-MM-DD>"
}
```
Later questions like "покажи Python-разработчиков с опытом 5+ и ссылки на их резюме" are then
answered from the `candidates` database via `query_knowledge_base`, returning summaries **and** resume links.

**Personal data:** resumes are personal data. Save only candidates the user explicitly selected;
never bulk-ingest an entire result set.

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
