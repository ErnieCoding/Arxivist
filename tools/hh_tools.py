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

from . import hh_auth

log = logging.getLogger(__name__)

HH_BASE_URL = "https://api.hh.ru"
HH_USER_AGENT = "Arxivist/1.0 (info@cyberskill.net)"


def _auth_link() -> str:
    """Relative path to the HH login route, resolved against the app origin."""
    base = os.environ.get("APP_BASE", "").rstrip("/")
    return f"{base}/hh/authorize"


def _headers() -> dict:
    h = {"User-Agent": HH_USER_AGENT, "Accept": "application/json"}
    # Read the token fresh each call so a token obtained via /hh/authorize
    # (or a refreshed one) is picked up without restarting the app.
    token = hh_auth.load_access_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
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
            if e.code == 403:
                return {"ok": False, "error": (
                    "HTTP 403 (forbidden). HeadHunter requires an access token for search "
                    "endpoints. Set HH_CLIENT_ID and HH_CLIENT_SECRET in .env (an application "
                    "token is then obtained automatically), or authorize a user token via /hh/authorize."
                )}
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
                "enum": ["areas", "professional_roles", "dictionaries", "skills"],
                "description": (
                    "What to retrieve: "
                    "'areas' = region tree with codes (public); "
                    "'professional_roles' = role taxonomy with IDs (public); "
                    "'dictionaries' = all enum values — experience, employment, schedule… (public); "
                    "'skills' = skill name autocomplete (requires 'query' param; may need auth)."
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


@tool(
    "search_hh_resumes",
    (
        "Search HeadHunter resumes / candidates. Requires a user OAuth token AND a paid employer "
        "CV-database subscription. If authorization is missing, this returns an 'authorization required' "
        "message containing a login link — relay that link to the user; do not retry."
    ),
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Search keywords (role, skills)."},
            "area": {"type": "integer", "description": "Region code (1=Москва, 2=СПб, 113=Россия)."},
            "experience": {
                "type": "string",
                "enum": ["noExperience", "between1And3", "between3And6", "moreThan6"],
                "description": "Candidate experience level.",
            },
            "salary_from": {"type": "integer", "description": "Minimum expected salary."},
            "per_page": {"type": "integer", "description": "Results per page (max 100, default 20)."},
            "page": {"type": "integer", "description": "Page number (0-indexed)."},
        },
    },
)
async def search_hh_resumes(args: dict) -> dict:
    # Resume search needs a USER token specifically. If none is present, ask
    # the user to authorize (client_credentials app tokens cannot read resumes).
    tok = hh_auth._read()
    has_user_token = bool(tok.get("refresh_token")) or tok.get("grant") == "authorization_code"
    if not has_user_token and not os.environ.get("HH_ACCESS_TOKEN"):
        link = _auth_link()
        return {"content": [{"type": "text", "text": (
            "AUTHORIZATION_REQUIRED: Resume search needs a one-time HeadHunter sign-in. "
            f"Present this login link to the user (in their language), e.g. [Войти через HeadHunter]({link}). "
            "After they sign in once, resume search will work. Do not retry until they have signed in."
        )}]}

    params: dict = {
        "per_page": min(int(args.get("per_page") or 20), 100),
        "page": int(args.get("page") or 0),
    }
    for k_src, k_dst in (("text", "text"), ("area", "area"), ("experience", "experience"), ("salary_from", "salary_from")):
        if args.get(k_src) is not None:
            params[k_dst] = args[k_src]

    log.info("HH resume search: %s", params)
    result = _get("/resumes", params=params)

    if not result["ok"]:
        # A 403 here means the token lacks resume access (no paid CV subscription)
        # or a user login is still required.
        link = _auth_link()
        if "403" in str(result["error"]):
            return {"content": [{"type": "text", "text": (
                "Resume search is unavailable: the HeadHunter account needs a paid CV-database "
                "subscription and a user sign-in. If not signed in yet, show the user: "
                f"[Войти через HeadHunter]({link})."
            )}]}
        return {"content": [{"type": "text", "text": f"Resume search failed: {result['error']}"}]}

    data = result["data"]
    items = data.get("items", [])
    found = data.get("found", 0)
    if not items:
        return {"content": [{"type": "text", "text": "Резюме по заданным критериям не найдены."}]}

    # Numbered list so the user can pick which candidates to save ("save #1, #3").
    # Each entry carries the resume link (user-facing) — the agent can derive the
    # resume_id from that URL later when saving to the knowledge base.
    lines = [f"Найдено резюме: {found} (показано {len(items)}).\n"]
    for i, r in enumerate(items, 1):
        title = r.get("title", "—")
        area = (r.get("area") or {}).get("name", "?")
        age = r.get("age")
        exp = (r.get("total_experience") or {}).get("months")
        exp_str = f"{exp // 12} лет" if exp else "опыт не указан"
        roles = ", ".join(pr.get("name", "") for pr in (r.get("professional_roles") or [])[:3])
        url = r.get("alternate_url", "")
        parts = [f"{i}. {title} — {area}, {exp_str}"]
        if age:
            parts.append(f"{age} лет")
        if r.get("salary"):
            parts.append(_salary_str(r.get("salary")))
        line = " | ".join(parts)
        if roles:
            line += f"\n   Роли: {roles}"
        line += f"\n   Резюме: {url}"
        lines.append(line)

    lines.append(
        "\n[Present this to the user as a clean numbered list with the resume links. "
        "Then offer to save selected candidates to the knowledge base. When the user picks some, "
        "for each: optionally call get_hh_resume to enrich, then add_document_to_kb into the "
        "'candidates' database with resume_url as a field. Do not save anything unless asked.]"
    )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "get_hh_resume",
    (
        "Fetch full details of a single HeadHunter resume by its resume_id (the ID at the end of a "
        "resume URL like https://hh.ru/resume/<id>). Use this to enrich a candidate before saving to "
        "the knowledge base. Requires a user token + paid CV-database access."
    ),
    {
        "type": "object",
        "properties": {
            "resume_id": {
                "type": "string",
                "description": "Resume ID — the last path segment of the resume URL (hh.ru/resume/<id>).",
            },
        },
        "required": ["resume_id"],
    },
)
async def get_hh_resume(args: dict) -> dict:
    rid = args["resume_id"]
    result = _get(f"/resumes/{rid}")
    if not result["ok"]:
        link = _auth_link()
        if "403" in str(result["error"]):
            return {"content": [{"type": "text", "text": (
                "Access to this resume is restricted (needs a signed-in employer account with a paid "
                f"CV-database subscription). If not signed in yet, show: [Войти через HeadHunter]({link})."
            )}]}
        return {"content": [{"type": "text", "text": f"Failed to fetch resume {rid}: {result['error']}"}]}

    r = result["data"]
    area = (r.get("area") or {}).get("name", "?")
    exp_months = (r.get("total_experience") or {}).get("months")
    exp_years = f"{exp_months // 12} лет" if exp_months else "не указан"
    skills = r.get("skill_set") or []
    roles = ", ".join(pr.get("name", "") for pr in (r.get("professional_roles") or []))
    education = r.get("education") or {}
    edu_level = (education.get("level") or {}).get("name", "")
    experience = r.get("experience") or []
    exp_lines = []
    for e in experience[:6]:
        company = e.get("company", "?")
        position = e.get("position", "?")
        start = (e.get("start") or "")[:7]
        end = (e.get("end") or "по наст. время")[:7] if e.get("end") else "по наст. время"
        exp_lines.append(f"  • {position} — {company} ({start}–{end})")

    out = [
        f"Резюме: {r.get('title', '—')}",
        f"Регион: {area} | Общий опыт: {exp_years}",
        f"Зарплатные ожидания: {_salary_str(r.get('salary'))}",
        f"Роли: {roles or 'не указаны'}",
        f"Образование: {edu_level or 'не указано'}",
        f"Ключевые навыки: {', '.join(skills) if skills else 'не указаны'}",
        f"Ссылка: {r.get('alternate_url', '')}",
    ]
    if exp_lines:
        out.append("Опыт работы:\n" + "\n".join(exp_lines))
    return {"content": [{"type": "text", "text": "\n".join(out)}]}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

hh_server = create_sdk_mcp_server(
    name="hh",
    version="1.1.0",
    tools=[
        search_hh_vacancies,
        get_hh_vacancy,
        search_hh_employers,
        get_hh_employer_details,
        get_hh_reference,
        search_hh_resumes,
        get_hh_resume,
    ],
)
