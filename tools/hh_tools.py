"""
HeadHunter API MCP tools — typed, deterministic wrappers around api.hh.ru.

Public endpoints (vacancy search, employers, reference data) work without auth.
Resume search and candidate contacts require HH_ACCESS_TOKEN (OAuth token from
a registered HH application at https://dev.hh.ru/).

Rate limits: ~10 req/sec without auth, higher with auth. Built-in retry on 429.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from claude_agent_sdk import tool, create_sdk_mcp_server

log = logging.getLogger(__name__)

HH_BASE_URL = "https://api.hh.ru"
HH_USER_AGENT = "Arxivist/1.0 (info@cyberskill.net)"
HH_ACCESS_TOKEN = os.environ.get("HH_ACCESS_TOKEN", "")


def _headers() -> dict:
    h = {"User-Agent": HH_USER_AGENT, "Accept": "application/json"}
    if HH_ACCESS_TOKEN:
        h["Authorization"] = f"Bearer {HH_ACCESS_TOKEN}"
    return h


def _get(path: str, params: dict | None = None, timeout: int = 30) -> dict:
    """GET request to HH API with retry on 429."""
    clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None}
    url = f"{HH_BASE_URL}{path}"
    if clean_params:
        url = f"{url}?{urllib.parse.urlencode(clean_params, encoding='utf-8')}"

    for attempt in range(3):
        req = urllib.request.Request(url, headers=_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {"ok": True, "data": json.loads(resp.read().decode("utf-8"))}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                log.warning("HH rate limit (429), waiting 5s …")
                time.sleep(5)
                continue
            err = ""
            try:
                err = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            log.error("HH API HTTP %d %s: %s", e.code, path, err)
            return {"ok": False, "error": f"HTTP {e.code}: {err}"}
        except Exception as e:
            log.error("HH API error %s: %s", path, e)
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": "Max retries exceeded (repeated 429 rate limit)"}


def _salary_str(salary) -> str:
    if not salary:
        return "не указана"
    parts = []
    if salary.get("from"):
        parts.append(f"от {salary['from']:,}")
    if salary.get("to"):
        parts.append(f"до {salary['to']:,}")
    cur = salary.get("currency", "")
    gross = " (до вычета налогов)" if salary.get("gross") else ""
    return (" ".join(parts) + f" {cur}{gross}") if parts else "не указана"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(
    "search_hh_vacancies",
    (
        "Search HeadHunter for job vacancies. Returns formatted list with salary, company, "
        "location, and URL. Use this to find open positions, analyse hiring trends, or "
        "collect data for knowledge base enrichment."
    ),
    {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Search keywords (role name, skills, etc.). Supports Russian and English.",
            },
            "area": {
                "type": "integer",
                "description": "Region code. Common values: 1=Москва, 2=Санкт-Петербург, 113=Россия (вся). Call get_hh_reference with type='areas' for other regions.",
            },
            "employer_id": {
                "type": "integer",
                "description": "Filter by specific employer (company ID). Use search_hh_employers to find the ID.",
            },
            "experience": {
                "type": "string",
                "enum": ["noExperience", "between1And3", "between3And6", "moreThan6"],
                "description": "Required experience level.",
            },
            "employment": {
                "type": "string",
                "enum": ["full", "part", "project", "volunteer", "probation"],
                "description": "Employment type.",
            },
            "schedule": {
                "type": "string",
                "enum": ["fullDay", "shift", "flexible", "remote", "flyInFlyOut"],
                "description": "Work schedule.",
            },
            "salary": {
                "type": "integer",
                "description": "Minimum salary filter (in RUB).",
            },
            "order_by": {
                "type": "string",
                "enum": ["relevance", "publication_time", "salary_desc", "salary_asc"],
                "description": "Sort order. Default: publication_time (newest first).",
            },
            "per_page": {
                "type": "integer",
                "description": "Results per page. Max 100, default 20.",
            },
            "page": {
                "type": "integer",
                "description": "Page number (0-indexed). Default 0.",
            },
            "date_from": {
                "type": "string",
                "description": "Published after this date (ISO 8601, e.g. 2025-01-01T00:00:00+0300).",
            },
        },
    },
)
async def search_hh_vacancies(args: dict) -> dict:
    params: dict = {
        "per_page": min(int(args.get("per_page") or 20), 100),
        "page": int(args.get("page") or 0),
        "order_by": args.get("order_by") or "publication_time",
    }
    for k in ("text", "area", "employer_id", "experience", "employment", "schedule", "salary", "date_from"):
        if args.get(k) is not None:
            params[k] = args[k]

    log.info("HH vacancy search: %s", params)
    result = _get("/vacancies", params=params)

    if not result["ok"]:
        return {"content": [{"type": "text", "text": f"HH vacancy search failed: {result['error']}"}]}

    data = result["data"]
    items = data.get("items", [])
    found = data.get("found", 0)
    pages = data.get("pages", 1)
    current_page = data.get("page", 0)

    if not items:
        return {"content": [{"type": "text", "text": "Вакансии не найдены по заданным критериям."}]}

    lines = [f"Найдено: {found} вакансий (страница {current_page + 1}/{pages}, показано {len(items)})\n"]
    for i, v in enumerate(items, 1):
        employer = v.get("employer") or {}
        snippet = v.get("snippet") or {}
        lines.append(
            f"{i}. [{v.get('name', 'Без названия')}]({v.get('alternate_url', '')})\n"
            f"   Компания: {employer.get('name', 'не указана')}\n"
            f"   Зарплата: {_salary_str(v.get('salary'))}\n"
            f"   Регион: {(v.get('area') or {}).get('name', 'не указан')}\n"
            f"   Опубликовано: {v.get('published_at', '')[:10]}\n"
            f"   ID вакансии: {v.get('id')}\n"
            f"   Требования: {(snippet.get('requirement') or '').replace('<highlighttext>', '').replace('</highlighttext>', '')[:200]}\n"
        )

    if pages > 1 and current_page + 1 < pages:
        lines.append(f"\nДля следующей страницы используй page={current_page + 1}.")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "get_hh_vacancy",
    "Get full details of a specific HeadHunter vacancy by ID, including description, requirements, and key skills.",
    {
        "type": "object",
        "properties": {
            "vacancy_id": {
                "type": "string",
                "description": "Vacancy ID (from search_hh_vacancies results).",
            },
        },
        "required": ["vacancy_id"],
    },
)
async def get_hh_vacancy(args: dict) -> dict:
    vid = args["vacancy_id"]
    result = _get(f"/vacancies/{vid}")
    if not result["ok"]:
        return {"content": [{"type": "text", "text": f"Failed to get vacancy {vid}: {result['error']}"}]}

    v = result["data"]
    employer = v.get("employer") or {}
    skills = [s.get("name") for s in (v.get("key_skills") or [])]
    experience = (v.get("experience") or {}).get("name", "не указан")
    employment = (v.get("employment") or {}).get("name", "не указан")
    schedule = (v.get("schedule") or {}).get("name", "не указан")

    # Strip HTML from description
    desc = v.get("description", "")
    import re
    desc_clean = re.sub(r"<[^>]+>", " ", desc).strip()
    desc_clean = re.sub(r" +", " ", desc_clean)[:1500]

    text = (
        f"Вакансия: {v.get('name')}\n"
        f"Компания: {employer.get('name')} (ID: {employer.get('id')})\n"
        f"Зарплата: {_salary_str(v.get('salary'))}\n"
        f"Регион: {(v.get('area') or {}).get('name')}\n"
        f"Опыт: {experience} | Занятость: {employment} | График: {schedule}\n"
        f"Опубликовано: {v.get('published_at', '')[:10]}\n"
        f"URL: {v.get('alternate_url')}\n\n"
        f"Ключевые навыки: {', '.join(skills) if skills else 'не указаны'}\n\n"
        f"Описание:\n{desc_clean}\n"
    )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "search_hh_employers",
    "Search HeadHunter for companies (employers) by name or type. Returns employer ID, name, and description.",
    {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Company name or keyword to search.",
            },
            "area": {
                "type": "integer",
                "description": "Region code. 1=Москва, 2=СПб, 113=Россия.",
            },
            "employer_type": {
                "type": "string",
                "description": "Type filter: company, agency, private_recruiter, etc.",
            },
            "per_page": {
                "type": "integer",
                "description": "Results per page. Max 100, default 20.",
            },
        },
    },
)
async def search_hh_employers(args: dict) -> dict:
    params: dict = {"per_page": min(int(args.get("per_page") or 20), 100)}
    if args.get("text"):
        params["text"] = args["text"]
    if args.get("area"):
        params["area"] = args["area"]
    if args.get("employer_type"):
        params["type"] = args["employer_type"]

    result = _get("/employers", params=params)
    if not result["ok"]:
        return {"content": [{"type": "text", "text": f"HH employer search failed: {result['error']}"}]}

    data = result["data"]
    items = data.get("items", [])
    found = data.get("found", 0)

    if not items:
        return {"content": [{"type": "text", "text": "Компании не найдены по заданным критериям."}]}

    lines = [f"Найдено: {found} компаний, показано {len(items)}\n"]
    for e in items:
        lines.append(
            f"- {e.get('name')} (ID: {e.get('id')})\n"
            f"  Тип: {e.get('type', 'не указан')} | Регион: {(e.get('area') or {}).get('name', '?')}\n"
            f"  URL: {e.get('alternate_url', '')}\n"
        )

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "get_hh_employer_details",
    (
        "Get full company profile from HeadHunter including description, size, industry, "
        "and count of open vacancies. Use employer ID from search_hh_employers."
    ),
    {
        "type": "object",
        "properties": {
            "employer_id": {
                "type": "string",
                "description": "Employer ID (from search_hh_employers).",
            },
            "include_vacancies": {
                "type": "boolean",
                "description": "If true, also fetch and include the list of open vacancies. Default: false.",
            },
        },
        "required": ["employer_id"],
    },
)
async def get_hh_employer_details(args: dict) -> dict:
    eid = args["employer_id"]
    include_vacancies = bool(args.get("include_vacancies", False))

    result = _get(f"/employers/{eid}")
    if not result["ok"]:
        return {"content": [{"type": "text", "text": f"Failed to get employer {eid}: {result['error']}"}]}

    e = result["data"]
    industries = ", ".join(i.get("name", "") for i in (e.get("industries") or []))
    open_vacancies = e.get("open_vacancies_count", "?")

    import re
    desc = re.sub(r"<[^>]+>", " ", e.get("description") or "").strip()
    desc = re.sub(r" +", " ", desc)[:1000]

    text = (
        f"Компания: {e.get('name')} (ID: {eid})\n"
        f"Сайт: {e.get('site_url', 'не указан')}\n"
        f"Тип: {e.get('type', 'не указан')}\n"
        f"Регион: {(e.get('area') or {}).get('name', 'не указан')}\n"
        f"Отрасли: {industries or 'не указаны'}\n"
        f"Открытых вакансий: {open_vacancies}\n"
        f"HH URL: {e.get('alternate_url', '')}\n\n"
        f"Описание:\n{desc or 'не указано'}\n"
    )

    if include_vacancies and open_vacancies and str(open_vacancies) != "0":
        vac_result = _get("/vacancies", params={"employer_id": eid, "per_page": 20})
        if vac_result["ok"]:
            vac_items = vac_result["data"].get("items", [])
            if vac_items:
                text += f"\nОткрытые вакансии ({len(vac_items)} из {open_vacancies}):\n"
                for v in vac_items:
                    text += f"  - {v.get('name')} ({_salary_str(v.get('salary'))}) — {v.get('alternate_url')}\n"

    return {"content": [{"type": "text", "text": text}]}


@tool(
    "get_hh_reference",
    (
        "Retrieve HH reference data: regions (areas), professional roles, specializations, "
        "work dictionaries (experience/employment/schedule types), or skill autocomplete. "
        "Use this to find region codes, role IDs, or valid enum values for search filters."
    ),
    {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["areas", "professional_roles", "specializations", "dictionaries", "skills"],
                "description": (
                    "What to retrieve: "
                    "'areas' = region tree with codes; "
                    "'professional_roles' = role taxonomy with IDs; "
                    "'specializations' = specialization groups; "
                    "'dictionaries' = all enum values (experience, employment, schedule…); "
                    "'skills' = skill name autocomplete (requires 'query' param)."
                ),
            },
            "query": {
                "type": "string",
                "description": "Skill search keyword. Required when type='skills'.",
            },
            "filter": {
                "type": "string",
                "description": "Optional filter term — only return items whose name contains this string (case-insensitive).",
            },
        },
        "required": ["type"],
    },
)
async def get_hh_reference(args: dict) -> dict:
    ref_type = args["type"]
    filter_term = (args.get("filter") or "").lower()

    path_map = {
        "areas": "/areas",
        "professional_roles": "/professional_roles",
        "specializations": "/specializations",
        "dictionaries": "/dictionaries",
        "skills": "/skills",
    }
    path = path_map.get(ref_type)
    if not path:
        return {"content": [{"type": "text", "text": f"Unknown reference type: {ref_type}"}]}

    params = {}
    if ref_type == "skills":
        q = args.get("query", "")
        if not q:
            return {"content": [{"type": "text", "text": "Error: 'query' parameter required for type='skills'"}]}
        params["text"] = q

    result = _get(path, params=params or None)
    if not result["ok"]:
        return {"content": [{"type": "text", "text": f"HH reference '{ref_type}' failed: {result['error']}"}]}

    data = result["data"]

    def _flatten_areas(nodes, depth=0) -> list[str]:
        lines = []
        for node in (nodes if isinstance(nodes, list) else []):
            name = node.get("name", "?")
            area_id = node.get("id", "?")
            if not filter_term or filter_term in name.lower():
                lines.append("  " * depth + f"{name} (id={area_id})")
            lines.extend(_flatten_areas(node.get("areas", []), depth + 1))
        return lines

    if ref_type == "areas":
        lines = _flatten_areas(data if isinstance(data, list) else [data])
        text = "Regions:\n" + "\n".join(lines[:200])
        if len(lines) > 200:
            text += f"\n… ({len(lines) - 200} more — use filter= to narrow)"

    elif ref_type == "professional_roles":
        cats = data.get("categories", data) if isinstance(data, dict) else data
        lines = []
        for cat in (cats if isinstance(cats, list) else []):
            cat_name = cat.get("name", "?")
            for role in cat.get("roles", []):
                name = role.get("name", "?")
                if not filter_term or filter_term in name.lower() or filter_term in cat_name.lower():
                    lines.append(f"  [{cat_name}] {name} (id={role.get('id')})")
        text = f"Professional roles ({len(lines)}):\n" + "\n".join(lines[:200])

    elif ref_type == "specializations":
        lines = []
        for group in (data if isinstance(data, list) else []):
            g_name = group.get("name", "?")
            for spec in group.get("specializations", []):
                name = spec.get("name", "?")
                if not filter_term or filter_term in name.lower() or filter_term in g_name.lower():
                    lines.append(f"  [{g_name}] {name} (id={spec.get('id')})")
        text = f"Specializations ({len(lines)}):\n" + "\n".join(lines[:200])

    elif ref_type == "dictionaries":
        lines = []
        for key, items in (data.items() if isinstance(data, dict) else {}.items()):
            if filter_term and filter_term not in key.lower():
                continue
            lines.append(f"\n{key}:")
            for item in (items if isinstance(items, list) else []):
                lines.append(f"  {item.get('id', '?')}: {item.get('name', '?')}")
        text = "Dictionaries:" + "\n".join(lines[:300])

    elif ref_type == "skills":
        items = data if isinstance(data, list) else data.get("items", [])
        text = f"Skills matching '{args.get('query')}':\n" + "\n".join(
            f"  - {s.get('text') or s.get('name') or s}" for s in items[:50]
        )

    else:
        text = json.dumps(data, ensure_ascii=False)[:3000]

    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

hh_server = create_sdk_mcp_server(
    name="hh",
    version="1.0.0",
    tools=[search_hh_vacancies, get_hh_vacancy, search_hh_employers, get_hh_employer_details, get_hh_reference],
)
