import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.error

# Fix Windows console encoding — agent SDK can emit emoji/unicode that
# crashes cp1252. Force UTF-8 before any output happens.
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import anthropic
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, jsonify, send_file, abort, redirect, url_for,
    Response, stream_with_context,
)

load_dotenv()

from claude_agent_sdk import query, ClaudeAgentOptions
from arxiv_tools import arxiv_server, DOWNLOADS_DIR, reset_session, get_session

from tools.call_api import api_server
from tools.dashboard_tools import dashboard_server
from tools.kb_tools import kb_server
from tools.hh_tools import hh_server
from tools import hh_auth

import dashboards_store
import uploads_store

app = Flask(__name__)

# Trust the nginx reverse proxy for correct client IP, protocol, and host headers.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


class _PrefixStripMiddleware:
    """
    Strip APP_BASE from PATH_INFO so the same Flask routes serve in two modes:

      1. Direct hit (no nginx): browser -> /arxivist/upload -> Flask sees /upload.
      2. nginx with `proxy_pass http://upstream/;` (trailing slash): nginx strips
         the prefix, Flask gets /upload directly, this middleware is a no-op.

    Without this, direct access 404s on every API endpoint because the frontend
    prepends APP_BASE to fetch URLs but Flask routes don't include the prefix.
    """

    def __init__(self, wsgi_app, prefix: str):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if self.prefix:
            path = environ.get("PATH_INFO", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                environ["PATH_INFO"] = path[len(self.prefix):] or "/"
                environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
        return self.wsgi_app(environ, start_response)


_APP_BASE_ENV = os.environ.get("APP_BASE", "").rstrip("/")
if _APP_BASE_ENV:
    app.wsgi_app = _PrefixStripMiddleware(app.wsgi_app, _APP_BASE_ENV)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# When deployed behind nginx at a sub-path (e.g. /arxivist), set SCRIPT_NAME
# in the container environment so the template generates correct fetch URLs.
SCRIPT_NAME = os.environ.get("APP_BASE", "").rstrip("/")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(PROJECT_DIR, ".claude", "skills"), exist_ok=True)
os.makedirs(os.path.join(PROJECT_DIR, "uploads"), exist_ok=True)
os.makedirs(os.path.join(PROJECT_DIR, "dashboards"), exist_ok=True)

DASHBOARD_UUID_RE = re.compile(r"^[a-f0-9]{32}$")

# Claude Agent SDK transport buffers each JSON-RPC message from the bundled
# CLI in memory. When the agent calls `Read` on a PDF, the CLI returns the
# PDF as a base64-encoded document block in one big message. 25 MB upload
# cap * 4/3 base64 inflation + JSON overhead can hit ~35 MB, so we give
# ourselves headroom. Default in the SDK is only 1 MiB.
AGENT_MAX_BUFFER_SIZE = 64 * 1024 * 1024  # 64 MiB


def _format_error_report(session: dict) -> str:
    """Build a markdown error report from session errors."""
    errors = session.get("errors", [])
    if not errors:
        return ""

    lines = ["**What went wrong:**\n"]
    for err in errors:
        lines.append(f"- **{err['stage']}** — {err['detail']}")
    return "\n".join(lines)


def summarize_papers(session: dict, user_query: str) -> str:
    """
    Use a direct Anthropic API call to produce summaries from the abstracts
    collected during the agent's search phase.
    """
    papers = session["papers"]
    downloaded = set(session["downloaded"])
    error_report = _format_error_report(session)

    if not papers:
        msg = "### No papers were found\n\n"
        if error_report:
            msg += error_report
        else:
            msg += (
                "The search returned no results. Try different or broader "
                "search terms, or check if arXiv is currently accessible."
            )
        return msg

    # Build context: only papers that were actually downloaded
    paper_entries = []
    for p in papers:
        safe_id = re.sub(r"[^\w.\-]", "_", p["arxiv_id"]) if p["arxiv_id"] else None
        filename = f"{safe_id}.pdf" if safe_id and f"{safe_id}.pdf" in downloaded else None

        if not filename:
            continue

        paper_entries.append(
            f"Title: {p['title']}\n"
            f"Authors: {', '.join(p['authors'])}\n"
            f"Category: {p['category']}\n"
            f"Published: {p['published'][:10]}\n"
            f"arXiv ID: {p['arxiv_id']}\n"
            f"Filename: {filename}\n"
            f"Abstract: {p['abstract']}\n"
        )

    if not paper_entries:
        msg = f"### {len(papers)} paper(s) found but none could be downloaded\n\n"
        if error_report:
            msg += error_report
        else:
            msg += "The downloads failed for an unknown reason. Try again shortly."
        return msg

    papers_block = "\n---\n".join(paper_entries)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=os.environ.get("SUMMARIZE_MODEL", "claude-sonnet-4-6"),
        max_tokens=8192,
        messages=[
            {
                "role": "user",
                "content": (
                    f"The user searched for: \"{user_query}\"\n\n"
                    f"The following {len(paper_entries)} papers were downloaded. "
                    f"For each paper, write a detailed overview using this exact format "
                    f"(use markdown formatting):\n\n"
                    f"### [number]. [filename]\n"
                    f"**Title:** [full title]\n\n"
                    f"**Authors:** [all author names]\n\n"
                    f"**Category:** [category] | **Published:** [date]\n\n"
                    f"**Summary:** [4-6 sentences providing a thorough overview: "
                    f"what problem the paper addresses, the proposed approach/method, "
                    f"key findings or contributions, and why it matters to the field. "
                    f"Write in plain language accessible to a technical audience.]\n\n"
                    f"---\n\n"
                    f"If the user's query is in a non-English language, write the "
                    f"summaries in that same language.\n\n"
                    f"Papers:\n\n{papers_block}"
                ),
            }
        ],
    )

    return response.content[0].text


async def run_agent(user_query: str, max_results: int, authors: str) -> None:
    """
    Run the agent loop: parse query, search arXiv, download papers.
    Papers and download state are tracked in the tools session.
    Does NOT produce summaries — that happens in summarize_papers().
    """
    author_instruction = ""
    if authors:
        author_instruction = (
            f"\nThe user also specified author filter(s): \"{authors}\". "
            f"Prepend each author surname with the arXiv author prefix, e.g. "
            f"au:LastName, and include them in the search query.\n"
        )

    prompt = (
        f"The user wants to find and download scientific papers from arXiv.\n\n"
        f"User query: \"{user_query}\"\n"
        f"Maximum results: {max_results}\n"
        f"{author_instruction}\n"
        f"Instructions:\n"
        f"1. Use the `search_arxiv` tool to search arXiv for papers matching the query. "
        f"Set max_results to {max_results}.\n"
        f"2. For each paper found, use the `download_paper` tool to download the PDF.\n\n"
        f"IMPORTANT: If the user's query is in a non-English language, you MUST translate "
        f"the search terms to English before calling search_arxiv (arXiv only indexes English).\n\n"
        f"If a tool call fails, you may retry it once. Do NOT use Bash to sleep — "
        f"the tools have built-in retry and backoff logic."
    )

    options = ClaudeAgentOptions(
        cwd=PROJECT_DIR,
        setting_sources=["user", "project"],
        mcp_servers={"arxiv": arxiv_server},
        allowed_tools=[
            "Skill",
            "mcp__arxiv__search_arxiv",
            "mcp__arxiv__download_paper",
            "mcp__arxiv__list_downloads",
        ],
        max_turns=20,
        max_buffer_size=AGENT_MAX_BUFFER_SIZE,
    )

    turn = 0
    async for message in query(prompt=prompt, options=options):
        turn += 1
        mtype = type(message).__name__
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if hasattr(block, "name"):
                    log.info("Agent turn %d: called tool %s", turn, block.name)
                elif hasattr(block, "text") and block.text:
                    snippet = block.text[:120].encode("ascii", "replace").decode()
                    log.info("Agent turn %d: text - %s", turn, snippet)
        else:
            log.info("Agent turn %d: %s", turn, mtype)


@app.route("/")
def index():
    return render_template("index.html", base=SCRIPT_NAME)


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json()
    user_query = data.get("query", "").strip()
    max_results = data.get("max_results", 10)
    authors = data.get("authors", "").strip()

    if not user_query:
        return jsonify({"error": "Please enter a search query."}), 400

    try:
        max_results = int(max_results)
        max_results = max(1, min(max_results, 50))
    except (ValueError, TypeError):
        max_results = 10

    log.info("Query received: %r | authors: %r | max_results: %d", user_query, authors, max_results)
    start = time.time()

    try:
        # Step 1: Reset session and run agent (search + download)
        reset_session()
        asyncio.run(run_agent(user_query, max_results, authors))

        session = get_session()
        log.info(
            "Agent finished: %d papers found, %d downloaded | arXiv query: %s",
            len(session["papers"]),
            len(session["downloaded"]),
            session["search_query"],
        )

        if not session["papers"]:
            log.warning("Agent finished but session has 0 papers - tools may not have been called")
        if not session["downloaded"]:
            log.warning("Agent finished but session has 0 downloads")

        # Step 2: Produce summaries via direct API call
        summaries = summarize_papers(session, user_query)

        elapsed = time.time() - start
        log.info("Full pipeline completed in %.2f seconds", elapsed)

        # Step 3: Return only files downloaded THIS query
        session_files = []
        for fname in session["downloaded"]:
            path = os.path.join(DOWNLOADS_DIR, fname)
            if os.path.exists(path):
                size_kb = os.path.getsize(path) / 1024
                session_files.append({"name": fname, "size_kb": round(size_kb, 1)})

        return jsonify({
            "result": summaries,
            "files": session_files,
            "search_query": session["search_query"],
            "elapsed_seconds": round(elapsed, 2),
        })
    except Exception as e:
        elapsed = time.time() - start
        log.error("Query failed after %.2f seconds: %s", elapsed, e)

        # Try to include session errors for context
        session = get_session()
        error_report = _format_error_report(session)
        if error_report:
            msg = f"### Request failed\n\n{error_report}"
        else:
            msg = f"### Request failed\n\n**Error:** {e}"

        return jsonify({
            "result": msg,
            "files": [],
            "search_query": session.get("search_query", ""),
            "elapsed_seconds": round(elapsed, 2),
        })


def build_chat_prompt(history: list, new_message: str) -> str:
    """Build the per-request prompt for /chat, including conversation history."""
    lines = []
    if history:
        lines.append("Conversation history:")
        for turn in history:
            role = "User" if turn.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {turn.get('content', '')}")
        lines.append("")
    lines.append(f"User: {new_message}")
    lines.append("")
    lines.append(
        "Complete the user's full request end-to-end. "
        "If this involves finding or downloading papers, use the searching-arxiv skill. "
        "If this involves building a dashboard/summary page/overview page from a document or text, "
        "use the creating-dashboards skill. If the user wants to modify a previously created dashboard "
        "(an Active dashboard UUID will be set in your context), use the editing-dashboards skill. "
        "If this involves a database or external API, check .claude/skills/ for a matching skill, "
        "or use the creating-skills skill to create one first. "
        "If papers were downloaded and the user wants summaries, use the summarizing-papers skill. "
        "Do not stop halfway — complete all steps before responding."
    )
    return "\n".join(lines)


# Progress phrases shown to the user WHILE the agent works. Keyed by tool name,
# value is (ru, en). These are the ONLY window into the agent's activity — the
# final answer never mentions tools. Keep them short and human.
_STATUS_PHRASES = {
    "WebSearch":                        ("Ищу в интернете…", "Searching the web…"),
    "WebFetch":                         ("Читаю источники…", "Reading sources…"),
    "mcp__arxiv__search_arxiv":         ("Ищу статьи на arXiv…", "Searching arXiv…"),
    "mcp__arxiv__download_paper":       ("Загружаю статьи…", "Downloading papers…"),
    "mcp__arxiv__list_downloads":       ("Проверяю загрузки…", "Checking downloads…"),
    "mcp__kb__list_kb_databases":       ("Проверяю базы знаний…", "Checking knowledge bases…"),
    "mcp__kb__query_knowledge_base":    ("Ищу в базе знаний…", "Searching the knowledge base…"),
    "mcp__kb__add_document_to_kb":      ("Сохраняю в базу знаний…", "Saving to the knowledge base…"),
    "mcp__kb__get_kb_task_status":      ("Обрабатываю…", "Processing…"),
    "mcp__hh__search_hh_vacancies":     ("Ищу вакансии на HeadHunter…", "Searching HeadHunter vacancies…"),
    "mcp__hh__get_hh_vacancy":          ("Смотрю вакансию…", "Fetching a vacancy…"),
    "mcp__hh__search_hh_employers":     ("Ищу компании на HeadHunter…", "Searching HeadHunter employers…"),
    "mcp__hh__get_hh_employer_details": ("Собираю данные о компании…", "Fetching company details…"),
    "mcp__hh__get_hh_reference":        ("Уточняю справочники…", "Loading reference data…"),
    "mcp__hh__search_hh_resumes":       ("Ищу резюме…", "Searching resumes…"),
    "mcp__hh__get_hh_resume":           ("Изучаю резюме…", "Reading a resume…"),
    "mcp__dashboard__create_dashboard": ("Собираю дашборд…", "Building the dashboard…"),
    "mcp__dashboard__extract_text":     ("Извлекаю текст…", "Extracting text…"),
    "mcp__api__call_api":               ("Обращаюсь к внешнему сервису…", "Contacting an external service…"),
    "Read":                             ("Читаю файл…", "Reading a file…"),
    "Write":                            ("Готовлю файл…", "Preparing a file…"),
    "Edit":                             ("Вношу изменения…", "Applying changes…"),
    "Bash":                             ("Выполняю…", "Working…"),
    "Skill":                            ("Готовлюсь…", "Getting ready…"),
}
_STATUS_FALLBACK = ("Обрабатываю запрос…", "Working…")


def _detect_lang(text: str) -> str:
    """Very small heuristic: any Cyrillic → 'ru', else 'en'."""
    for ch in text:
        if "Ѐ" <= ch <= "ӿ":
            return "ru"
    return "en"


def _status_phrase(tool_name: str, lang: str) -> str:
    ru, en = _STATUS_PHRASES.get(tool_name, _STATUS_FALLBACK)
    return ru if lang == "ru" else en


def _build_chat_options(session_id, active_dashboard_uuid, attached_files):
    """Build ClaudeAgentOptions (system prompt + MCP servers + allowed tools) for /chat."""
    context_lines = [f"Session ID: {session_id}"]
    if active_dashboard_uuid:
        context_lines.append(
            f"Active dashboard: {active_dashboard_uuid} (file: dashboards/{active_dashboard_uuid}/index.html)"
        )
    else:
        context_lines.append("Active dashboard: (none — there is no dashboard to edit in this session yet)")

    if attached_files:
        context_lines.append("Attached files (this turn):")
        for f in attached_files:
            char_count = f.get("char_count")
            char_str = f"{char_count} chars" if char_count is not None else "binary (PDF)"
            context_lines.append(
                f"- file_id={f['file_id']} | name={f['name']!r} | parse_mode={f['parse_mode']} "
                f"| {char_str} | read_path={f['read_path']}"
            )
    else:
        context_lines.append("Attached files: (none this turn)")

    context_block = "\n".join(context_lines)

    system_prompt = (
        "You are Arxivist, an intelligent research assistant with self-extending capabilities.\n\n"
        "## Output style (IMPORTANT — the user sees ONLY your final message):\n"
        "- Reply with the RESULT only, in the user's language. Clean, well-formatted markdown.\n"
        "- NEVER expose internal mechanics: no tool names, no MCP/skill names, no endpoint URLs, "
        "no database/collection/file names, no JSON payloads, no HTTP status codes, no 'I called…', "
        "no step-by-step narration of what you did.\n"
        "- Do not describe HOW you found something (web, knowledge base, HeadHunter, arXiv). Just present "
        "what you found. You may cite real external source URLs when relevant (e.g. article links).\n"
        "- No preambles like 'Let me…', 'I'll now…', 'Here's what I did'. Lead with the answer.\n"
        "- If something genuinely failed, say so briefly in plain language and what the user can do — "
        "without dumping raw errors or internal details.\n"
        "- Exception: sign-in links you are told to surface (e.g. HeadHunter authorization) MUST be shown.\n\n"
        "## Native MCP tools (use directly — no SKILL.md needed):\n\n"
        "Knowledge base (neo.rndl.ru:5001, ArangoDB graph):\n"
        "  mcp__kb__list_kb_databases        — list all databases\n"
        "  mcp__kb__query_knowledge_base     — ask a natural language question (CALL THIS FIRST)\n"
        "  mcp__kb__add_document_to_kb       — ingest a structured JSON document (auto-creates db)\n"
        "  mcp__kb__get_kb_task_status       — poll async task status\n"
        "  NOTE: The KB ingests STRUCTURED JSON, not raw prose. When enriching, build a JSON object "
        "with fields (company, industry, description, recent_news, vacancies, sources, …) and pass it "
        "as the `document` argument. Use the 'companies' database for company intelligence.\n\n"
        "HeadHunter API (hh.ru):\n"
        "  mcp__hh__search_hh_vacancies      — search job vacancies\n"
        "  mcp__hh__get_hh_vacancy           — get full vacancy details\n"
        "  mcp__hh__search_hh_employers      — find companies\n"
        "  mcp__hh__get_hh_employer_details  — company profile + vacancies\n"
        "  mcp__hh__get_hh_reference         — areas, roles, skills, dictionaries\n"
        "  mcp__hh__search_hh_resumes        — search resumes/candidates (needs one-time sign-in)\n"
        "  mcp__hh__get_hh_resume            — full details of one resume by id\n\n"
        "arXiv:\n"
        "  mcp__arxiv__search_arxiv, mcp__arxiv__download_paper, mcp__arxiv__list_downloads\n\n"
        "Dashboard:\n"
        "  mcp__dashboard__create_dashboard, mcp__dashboard__extract_text\n\n"
        "Generic HTTP (for any other API):\n"
        "  mcp__api__call_api\n\n"
        "## Skills (loaded from .claude/skills/ — for orchestration and unknown APIs):\n"
        "  - searching-arxiv, summarizing-papers — arXiv workflow\n"
        "  - reading-uploads — read uploaded files\n"
        "  - creating-dashboards, editing-dashboards — dashboard workflow\n"
        "  - enriching-knowledge-base — web research → structure → KB ingestion workflow\n"
        "  - creating-skills — write new SKILL.md for any API the user describes\n"
        "  - (any skill previously created in .claude/skills/)\n\n"
        "## Decision rules:\n\n"
        "1. COMPANY / TOPIC QUERY: Call mcp__kb__query_knowledge_base FIRST. "
        "Use database name matching the topic (try 'companies', or call list_kb_databases to discover). "
        "If the KB has useful data → present it. "
        "If KB returns nothing → tell the user what you found (or didn't find) and ASK if they want "
        "you to search the web and enrich the KB. Do NOT start web search automatically.\n\n"
        "2. HEADHUNTER: Use mcp__hh__* tools directly. No SKILL.md needed. If a tool reply starts "
        "with 'AUTHORIZATION_REQUIRED' or contains a sign-in link, present that link to the user as a "
        "clickable markdown link (in their language), tell them the search will continue automatically "
        "after they sign in (no need to write anything), and STOP — do not retry. It is a one-time login.\n\n"
        "2a. CANDIDATES / RESUMES: search_hh_resumes returns a numbered list; present it cleanly with the "
        "resume links and ASK which candidates to save (never save automatically). When the user picks some, "
        "for each selected candidate: derive the resume_id from its URL (hh.ru/resume/<id>), optionally call "
        "get_hh_resume to enrich, then save with add_document_to_kb into the 'candidates' database — one JSON "
        "document per candidate that MUST include resume_url plus the profile fields (candidate role, area, "
        "experience_years, key_skills, salary, education, source, saved_at). This keeps candidate and resume "
        "together so later questions return both a summary and the resume link. Treat resumes as personal data: "
        "save only what the user explicitly selected.\n\n"
        "3. DASHBOARD: use creating-dashboards skill; if active dashboard exists, use editing-dashboards.\n\n"
        "4. NEW / UNKNOWN API: If the user provides endpoint + auth details, use creating-skills to "
        "write a SKILL.md, then immediately use mcp__api__call_api following that skill.\n\n"
        "5. Complete the full request end-to-end without stopping halfway.\n\n"
        "----- SESSION CONTEXT -----\n"
        f"{context_block}\n"
        "---------------------------\n"
    )

    options = ClaudeAgentOptions(
        cwd=PROJECT_DIR,
        setting_sources=["user", "project"],
        mcp_servers={
            "arxiv": arxiv_server,
            "api": api_server,
            "dashboard": dashboard_server,
            "kb": kb_server,
            "hh": hh_server,
        },
        allowed_tools=[
            "Skill",
            "Read",
            "Write",
            "Edit",
            "Bash",
            "WebSearch",
            "WebFetch",
            # arXiv
            "mcp__arxiv__search_arxiv",
            "mcp__arxiv__download_paper",
            "mcp__arxiv__list_downloads",
            # Generic HTTP (for creating-skills and unknown APIs)
            "mcp__api__call_api",
            # Dashboard
            "mcp__dashboard__create_dashboard",
            "mcp__dashboard__extract_text",
            # Knowledge Base (read + JSON-document ingestion)
            "mcp__kb__list_kb_databases",
            "mcp__kb__query_knowledge_base",
            "mcp__kb__add_document_to_kb",
            "mcp__kb__get_kb_task_status",
            # HeadHunter (deterministic typed tools)
            "mcp__hh__search_hh_vacancies",
            "mcp__hh__get_hh_vacancy",
            "mcp__hh__search_hh_employers",
            "mcp__hh__get_hh_employer_details",
            "mcp__hh__get_hh_reference",
            "mcp__hh__search_hh_resumes",
            "mcp__hh__get_hh_resume",
        ],
        max_turns=60,
        system_prompt=system_prompt,
        max_buffer_size=AGENT_MAX_BUFFER_SIZE,
    )

    return options


async def run_chat_stream(prompt, options, lang):
    """
    Drive the agent loop and yield events:
      {"type": "status", "phrase": <str>}  — emitted when the agent starts a tool
      {"type": "final",  "text": <str>}    — the clean final answer text (last text-only turn)
    Only the last assistant turn WITHOUT tool calls is treated as the answer, so
    intermediate reasoning/narration never reaches the user.
    """
    final_text = ""
    turn = 0
    async for message in query(prompt=prompt, options=options):
        turn += 1
        if not (hasattr(message, "content") and isinstance(message.content, list)):
            continue
        turn_text = []
        used_tool = False
        for block in message.content:
            if hasattr(block, "name") and block.name:
                used_tool = True
                phrase = _status_phrase(block.name, lang)
                log.info("Chat turn %d: tool %s", turn, block.name)
                yield {"type": "status", "phrase": phrase}
            elif hasattr(block, "text") and block.text:
                turn_text.append(block.text)
        # A text-only assistant message is the (running) final answer; keep the latest.
        if turn_text and not used_tool:
            final_text = "\n\n".join(turn_text)

    yield {"type": "final", "text": final_text}


def _resolve_attached_files(file_ids: list) -> list:
    """Build the agent-facing list of attached-file descriptors from upload IDs."""
    resolved = []
    for fid in file_ids or []:
        meta = uploads_store.read_meta(fid)
        if not meta:
            continue
        ext = meta.get("ext", "")
        if meta.get("parse_mode") == "pdf-native":
            read_path = f"uploads/{fid}/original{ext}"
        else:
            read_path = f"uploads/{fid}/text.md"
        resolved.append({
            "file_id": fid,
            "name": meta.get("name", fid),
            "parse_mode": meta.get("parse_mode", "plain"),
            "char_count": meta.get("char_count"),
            "read_path": read_path,
        })
    return resolved


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.route("/chat", methods=["POST"])
def chat():
    """
    Streams Server-Sent Events:
      {"type":"status","phrase":"…"}  — progress phrases shown WHILE processing
      {"type":"result", ...}          — final answer + files (rendered as the reply)
    The final answer contains no internal mechanics (enforced by the system prompt).
    """
    data = request.get_json()
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    session_id = (data.get("session_id") or "").strip()
    attached_file_ids = data.get("attached_file_ids") or []

    if not message:
        return jsonify({"error": "Please enter a message."}), 400
    if not session_id:
        return jsonify({"error": "Missing session_id."}), 400

    attached_files = _resolve_attached_files(attached_file_ids)
    active_dashboard_uuid = dashboards_store.get_active(session_id)
    lang = _detect_lang(message)

    log.info(
        "Chat message received: %r | history=%d | session=%s | attached=%d | active_dashboard=%s",
        message, len(history), session_id, len(attached_files), active_dashboard_uuid,
    )

    def generate():
        start = time.time()
        try:
            reset_session()
            prompt = build_chat_prompt(history, message)
            options = _build_chat_options(session_id, active_dashboard_uuid, attached_files)

            yield _sse({"type": "status", "phrase": "Думаю…" if lang == "ru" else "Thinking…"})

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            final_text = ""
            try:
                agen = run_chat_stream(prompt, options, lang)
                while True:
                    try:
                        event = loop.run_until_complete(agen.__anext__())
                    except StopAsyncIteration:
                        break
                    if event["type"] == "status":
                        yield _sse(event)
                    elif event["type"] == "final":
                        final_text = event["text"]
            finally:
                loop.close()

            session = get_session()
            log.info(
                "Chat agent finished: %d papers found, %d downloaded",
                len(session["papers"]), len(session["downloaded"]),
            )

            # Decide the reply: dashboard link > structured paper summary > agent text.
            new_active = dashboards_store.get_active(session_id)
            produced_dashboard = (
                (new_active and new_active != active_dashboard_uuid) or ("/d/" in final_text)
            )

            if produced_dashboard and final_text.strip():
                reply = final_text
            elif session["papers"] and session["downloaded"]:
                yield _sse({"type": "status", "phrase": "Готовлю обзор…" if lang == "ru" else "Summarizing…"})
                reply = summarize_papers(session, message)
            elif final_text.strip():
                reply = final_text
            else:
                error_report = _format_error_report(session)
                if error_report:
                    reply = error_report
                else:
                    reply = ("Не удалось выполнить запрос. Попробуйте переформулировать или добавить детали."
                             if lang == "ru" else
                             "I couldn't complete that request. Please try again or add detail.")

            elapsed = time.time() - start
            log.info("Chat pipeline completed in %.2f seconds", elapsed)

            session_files = []
            for fname in session["downloaded"]:
                path = os.path.join(DOWNLOADS_DIR, fname)
                if os.path.exists(path):
                    size_kb = os.path.getsize(path) / 1024
                    session_files.append({"name": fname, "size_kb": round(size_kb, 1)})

            yield _sse({
                "type": "result",
                "reply": reply,
                "files": session_files,
                "search_query": session.get("search_query", ""),
                "elapsed_seconds": round(elapsed, 2),
            })

        except Exception as e:
            elapsed = time.time() - start
            log.exception("Chat failed after %.2fs", elapsed)
            session = get_session()
            error_report = _format_error_report(session)
            if error_report:
                reply = error_report
            else:
                reply = ("Произошла ошибка при обработке запроса. Попробуйте ещё раз."
                         if lang == "ru" else
                         "Something went wrong while processing your request. Please try again.")
            yield _sse({
                "type": "result",
                "reply": reply,
                "files": [],
                "search_query": session.get("search_query", ""),
                "elapsed_seconds": round(elapsed, 2),
            })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx response buffering for SSE
        },
    )


@app.route("/files")
def list_all_files():
    files = []
    if os.path.exists(DOWNLOADS_DIR):
        for f in sorted(os.listdir(DOWNLOADS_DIR)):
            if f.lower().endswith(".pdf"):
                path = os.path.join(DOWNLOADS_DIR, f)
                size_kb = os.path.getsize(path) / 1024
                files.append({"name": f, "size_kb": round(size_kb, 1)})
    return jsonify({"files": files})


@app.route("/upload", methods=["POST"])
def upload():
    """Accept one or more file attachments. Returns per-file metadata."""
    if "file" not in request.files:
        return jsonify({"error": "No file part in request."}), 400

    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "No files attached."}), 400

    saved = []
    errors = []
    for fs in files:
        if not fs or not fs.filename:
            errors.append({"name": "?", "detail": "empty filename"})
            continue
        try:
            meta = uploads_store.save_upload(fs)
        except uploads_store.UploadError as e:
            errors.append({"name": fs.filename, "detail": str(e)})
        except Exception as e:
            log.exception("Upload failed for %s", fs.filename)
            errors.append({"name": fs.filename, "detail": f"Upload failed: {e}"})
        else:
            saved.append(meta)

    log.info("Upload received: %d saved, %d errors", len(saved), len(errors))
    return jsonify({"files": saved, "errors": errors})


@app.route("/d/<dashboard_uuid>")
def serve_dashboard(dashboard_uuid):
    """Serve a previously generated dashboard's HTML."""
    if not DASHBOARD_UUID_RE.match(dashboard_uuid):
        abort(404)
    path = dashboards_store.path_for(dashboard_uuid)
    if not path:
        abort(404)
    resp = send_file(path, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/dashboards")
def list_dashboards():
    """Return all registered dashboards for the sidebar (newest first)."""
    return jsonify({"dashboards": dashboards_store.list_all()})


_BRIDGE_BASE = f"http://{os.environ.get('BRIDGE_FAKE_HOST', '127.0.0.1')}:{os.environ.get('BRIDGE_FAKE_PORT', '9999')}"


@app.route("/api/proxy-mode", methods=["GET", "POST"])
def proxy_mode():
    """
    GET  → returns {"mode": "direct"|"proxy", "proxy_available": bool, "direct_available": bool}
    POST {"mode": "direct"|"proxy"} → switches the bridge mode, returns same shape
    """
    bridge_url = f"{_BRIDGE_BASE}/mode"
    try:
        if request.method == "POST":
            data = request.get_json(force=True) or {}
            payload = json.dumps({"mode": data.get("mode", "")}).encode("utf-8")
            req = urllib.request.Request(
                bridge_url, data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
        else:
            req = urllib.request.Request(bridge_url, method="GET")

        with urllib.request.urlopen(req, timeout=5) as resp:
            return jsonify(json.loads(resp.read()))

    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return jsonify({"error": f"Bridge returned HTTP {e.code}: {err}"}), 502
    except Exception as e:
        return jsonify({"error": f"Bridge unreachable: {e}"}), 502


# ---------------------------------------------------------------------------
# HeadHunter OAuth2 — one-time browser flow to obtain a user access token.
#   /hh/authorize → redirects to hh.ru consent screen
#   /hh/callback  → hh.ru redirects back here with ?code=…, we exchange + store
#   /hh/status    → JSON status of the current token
# The token is persisted to config/hh_token.json and read by tools/hh_tools.py.
# ---------------------------------------------------------------------------

def _hh_redirect_uri() -> str:
    """
    The redirect URI to hand HH. An explicit HH_REDIRECT_URI env wins (pin it
    if your registered value differs); otherwise derive it from the current
    request so the same code works on localhost and behind the nginx sub-path.
    Must match a Redirect URI registered in the HH app.
    """
    return hh_auth.HH_REDIRECT_URI or url_for("hh_callback", _external=True)


def _hh_page(title: str, body_html: str, status: int = 200, extra_script: str = ""):
    """Small dark-themed page shell for the OAuth screens, matching the app UI."""
    home = (SCRIPT_NAME or "") + "/"
    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ background:#0f1117; color:#e0e0e0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:#1a1d27; border:1px solid #2a2d3a; border-radius:14px; padding:2.2rem 2.6rem;
          max-width:520px; text-align:center; box-shadow:0 8px 32px rgba(0,0,0,.35); }}
  h2 {{ margin:0 0 .8rem; color:#fff; font-size:1.25rem; }}
  p  {{ margin:.4rem 0; line-height:1.55; color:#b9c0d4; font-size:.92rem; }}
  a  {{ color:#93b0ff; }}
  .ok {{ color:#4ade80; font-size:2.2rem; display:block; margin-bottom:.6rem; }}
  .err {{ color:#f87171; font-size:2.2rem; display:block; margin-bottom:.6rem; }}
  .hint {{ color:#6b7288; font-size:.8rem; margin-top:1.1rem; }}
</style></head>
<body><div class="card">{body_html}
<p class="hint"><a href="{home}">← Вернуться в Arxivist</a></p>
</div>{extra_script}</body></html>"""
    return html, status


@app.route("/hh/authorize")
def hh_authorize():
    if not hh_auth.HH_CLIENT_ID:
        return _hh_page(
            "HeadHunter — не настроено",
            "<span class='err'>✕</span><h2>Интеграция не настроена</h2>"
            "<p>HH_CLIENT_ID / HH_CLIENT_SECRET не заданы в .env. Задайте их и перезапустите приложение.</p>",
            status=400,
        )
    state = hh_auth.new_state()
    return redirect(hh_auth.build_authorize_url(redirect_uri=_hh_redirect_uri(), state=state))


@app.route("/hh/callback")
def hh_callback():
    error = request.args.get("error")
    if error:
        desc = request.args.get("error_description", "")
        return _hh_page(
            "HeadHunter — ошибка",
            f"<span class='err'>✕</span><h2>Авторизация не завершилась</h2><p>{error}: {desc}</p>"
            "<p>Вернитесь в чат и попробуйте войти ещё раз по ссылке от ассистента.</p>",
            status=400,
        )

    code = request.args.get("code")
    if not code:
        return _hh_page(
            "HeadHunter — ошибка",
            "<span class='err'>✕</span><h2>Не получен код авторизации</h2>"
            "<p>Похоже, страница открыта напрямую. Начните вход по ссылке от ассистента.</p>",
            status=400,
        )

    # CSRF check: the state must match the one minted at /hh/authorize.
    if not hh_auth.consume_state(request.args.get("state", "")):
        return _hh_page(
            "HeadHunter — ошибка",
            "<span class='err'>✕</span><h2>Сессия авторизации устарела</h2>"
            "<p>Ссылка входа была открыта повторно или истекла. Вернитесь в чат и начните вход заново.</p>",
            status=400,
        )

    result = hh_auth.exchange_code(code, redirect_uri=_hh_redirect_uri())
    if not result["ok"]:
        log.error("HH OAuth token exchange failed: %s", result["error"])
        return _hh_page(
            "HeadHunter — ошибка",
            "<span class='err'>✕</span><h2>Не удалось обменять код на токен</h2>"
            "<p>Попробуйте войти ещё раз. Если ошибка повторяется — проверьте, что Redirect URI "
            "в настройках приложения на dev.hh.ru совпадает с адресом этой страницы.</p>",
            status=502,
        )

    log.info("HH OAuth: user token obtained and stored")
    # Auto-return: notify the chat tab (same-origin BroadcastChannel) and close
    # this tab. The chat page ALSO polls /hh/status as a fallback, so the flow
    # continues even if this tab can't signal (different origin) or won't close.
    script = """<script>
  try { new BroadcastChannel('arxivist-hh-auth').postMessage({ type: 'hh-connected' }); } catch (e) {}
  setTimeout(function () { window.close(); }, 1200);
</script>"""
    return _hh_page(
        "HeadHunter подключён",
        "<span class='ok'>✓</span><h2>HeadHunter подключён</h2>"
        "<p>Поиск кандидатов и резюме теперь доступен ассистенту.</p>"
        "<p>Эта вкладка закроется сама — диалог продолжится в чате автоматически.</p>",
        extra_script=script,
    )


@app.route("/hh/status")
def hh_status():
    return jsonify(hh_auth.token_status())


@app.route("/hh/app-token", methods=["POST"])
def hh_app_token():
    """Obtain a client_credentials application token (no user interaction)."""
    result = hh_auth.get_app_token()
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 502
    return jsonify({"ok": True, "status": hh_auth.token_status()})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
