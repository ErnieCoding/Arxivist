"""
Shared agent pipeline — the single implementation of "run the Arxivist agent
and produce a clean result", used by three consumers:

  1. app.py /chat        — browser debug UI (wraps events into SSE)
  2. job_runner.py       — background runner for API agent jobs (writes events
                           to the job's events file)
  3. api_v1.py           — POST /api/v1/arxiv/search (run_search_pipeline)

Extracted from app.py so the API and the chat UI can never drift apart: both
drive the exact same prompt, options, agent loop, and reply-decision logic.

Concurrency note: this module (via arxiv_tools._session) is NOT thread-safe.
Each consumer runs it in its own PROCESS (gunicorn sync worker or a dedicated
runner process) — never in threads.
"""

import asyncio
import logging
import os
import re
import time

import anthropic

from claude_agent_sdk import query, ClaudeAgentOptions
from arxiv_tools import arxiv_server, DOWNLOADS_DIR, reset_session, get_session

from tools.call_api import api_server
from tools.dashboard_tools import dashboard_server
from tools.kb_tools import kb_server
from tools.hh_tools import hh_server

import dashboards_store
import uploads_store

log = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Claude Agent SDK transport buffers each JSON-RPC message from the bundled
# CLI in memory. When the agent calls `Read` on a PDF, the CLI returns the
# PDF as a base64-encoded document block in one big message. 25 MB upload
# cap * 4/3 base64 inflation + JSON overhead can hit ~35 MB, so we give
# ourselves headroom. Default in the SDK is only 1 MiB.
AGENT_MAX_BUFFER_SIZE = 64 * 1024 * 1024  # 64 MiB


# ---------------------------------------------------------------------------
# Language + status phrases
# ---------------------------------------------------------------------------

# Progress phrases shown to the user WHILE the agent works. Keyed by tool name,
# value is (ru, en). These are the ONLY window into the agent's activity — the
# final answer never mentions tools. Keep them short and human.
STATUS_PHRASES = {
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
STATUS_FALLBACK = ("Обрабатываю запрос…", "Working…")


def detect_lang(text: str) -> str:
    """Very small heuristic: any Cyrillic → 'ru', else 'en'."""
    for ch in text:
        if "Ѐ" <= ch <= "ӿ":
            return "ru"
    return "en"


def status_phrase(tool_name: str, lang: str) -> str:
    ru, en = STATUS_PHRASES.get(tool_name, STATUS_FALLBACK)
    return ru if lang == "ru" else en


# ---------------------------------------------------------------------------
# Error report + paper summaries (direct API call, not agent)
# ---------------------------------------------------------------------------

def format_error_report(session: dict) -> str:
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
    error_report = format_error_report(session)

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


# ---------------------------------------------------------------------------
# Structured arXiv search pipeline (/search route + POST /api/v1/arxiv/search)
# ---------------------------------------------------------------------------

async def _run_search_agent(user_query: str, max_results: int, authors: str) -> None:
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


def session_files_list(session: dict) -> list[dict]:
    """Files downloaded during THIS session, with sizes (for the response)."""
    out = []
    for fname in session["downloaded"]:
        path = os.path.join(DOWNLOADS_DIR, fname)
        if os.path.exists(path):
            size_kb = os.path.getsize(path) / 1024
            out.append({"name": fname, "size_kb": round(size_kb, 1)})
    return out


def run_search_pipeline(user_query: str, max_results: int, authors: str) -> dict:
    """
    Full structured search: agent phase (search + download) + deterministic
    summaries. Never raises — errors are folded into the returned markdown.
    Returns {result, files, search_query, elapsed_seconds}.
    """
    start = time.time()
    try:
        reset_session()
        asyncio.run(_run_search_agent(user_query, max_results, authors))

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

        summaries = summarize_papers(session, user_query)

        elapsed = time.time() - start
        log.info("Full pipeline completed in %.2f seconds", elapsed)

        return {
            "result": summaries,
            "files": session_files_list(session),
            "search_query": session["search_query"],
            "elapsed_seconds": round(elapsed, 2),
        }
    except Exception as e:
        elapsed = time.time() - start
        log.error("Query failed after %.2f seconds: %s", elapsed, e)

        session = get_session()
        error_report = format_error_report(session)
        if error_report:
            msg = f"### Request failed\n\n{error_report}"
        else:
            msg = f"### Request failed\n\n**Error:** {e}"

        return {
            "result": msg,
            "files": [],
            "search_query": session.get("search_query", ""),
            "elapsed_seconds": round(elapsed, 2),
        }


# ---------------------------------------------------------------------------
# Chat/agent pipeline (browser /chat + API agent jobs)
# ---------------------------------------------------------------------------

def build_chat_prompt(history: list, new_message: str) -> str:
    """Build the per-request prompt, including conversation history."""
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


def build_chat_options(session_id, active_dashboard_uuid, attached_files):
    """Build ClaudeAgentOptions (system prompt + MCP servers + allowed tools)."""
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
                phrase = status_phrase(block.name, lang)
                log.info("Chat turn %d: tool %s", turn, block.name)
                yield {"type": "status", "phrase": phrase}
            elif hasattr(block, "text") and block.text:
                turn_text.append(block.text)
        # A text-only assistant message is the (running) final answer; keep the latest.
        if turn_text and not used_tool:
            final_text = "\n\n".join(turn_text)

    yield {"type": "final", "text": final_text}


def resolve_attached_files(file_ids: list) -> list:
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


def pipeline_events(message: str, history: list, session_id: str, attached_file_ids: list):
    """
    SYNC generator running the full chat pipeline. Yields event dicts:
      {"type": "status", "phrase": str}                     — progress
      {"type": "result", "reply", "files", "search_query",
       "elapsed_seconds"}                                   — final (always last)
    Never raises — failures are folded into a result event. Runs the async
    agent loop on a fresh event loop (no threads; caller must be a process
    that owns arxiv_tools._session for the duration).
    """
    start = time.time()
    lang = detect_lang(message)
    attached_files = resolve_attached_files(attached_file_ids)
    active_dashboard_uuid = dashboards_store.get_active(session_id)

    try:
        reset_session()
        prompt = build_chat_prompt(history, message)
        options = build_chat_options(session_id, active_dashboard_uuid, attached_files)

        yield {"type": "status", "phrase": "Думаю…" if lang == "ru" else "Thinking…"}

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
                    yield event
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
            yield {"type": "status", "phrase": "Готовлю обзор…" if lang == "ru" else "Summarizing…"}
            reply = summarize_papers(session, message)
        elif final_text.strip():
            reply = final_text
        else:
            error_report = format_error_report(session)
            if error_report:
                reply = error_report
            else:
                reply = ("Не удалось выполнить запрос. Попробуйте переформулировать или добавить детали."
                         if lang == "ru" else
                         "I couldn't complete that request. Please try again or add detail.")

        elapsed = time.time() - start
        log.info("Chat pipeline completed in %.2f seconds", elapsed)

        yield {
            "type": "result",
            "reply": reply,
            "files": session_files_list(session),
            "search_query": session.get("search_query", ""),
            "elapsed_seconds": round(elapsed, 2),
        }

    except Exception:
        elapsed = time.time() - start
        log.exception("Chat failed after %.2fs", elapsed)
        session = get_session()
        error_report = format_error_report(session)
        if error_report:
            reply = error_report
        else:
            reply = ("Произошла ошибка при обработке запроса. Попробуйте ещё раз."
                     if lang == "ru" else
                     "Something went wrong while processing your request. Please try again.")
        yield {
            "type": "result",
            "reply": reply,
            "files": [],
            "search_query": session.get("search_query", ""),
            "elapsed_seconds": round(elapsed, 2),
        }
