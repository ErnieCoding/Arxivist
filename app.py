"""
Flask web app that accepts natural language queries and uses the Claude Agent SDK
to search arXiv, download papers, and then produces summaries via a direct API call.

Pipeline per query:
  1. Agent loop: parse query -> search_arxiv -> download_paper (per result)
  2. Post-agent: direct anthropic API call to summarize using collected abstracts
  3. Return summaries + list of files downloaded THIS query only
"""

import asyncio
import logging
import os
import re
import sys
import time

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
from flask import Flask, render_template, request, jsonify, send_file, abort

load_dotenv()

from claude_agent_sdk import query, ClaudeAgentOptions
from tools import arxiv_server, DOWNLOADS_DIR, reset_session, get_session

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
from call_api import api_server
from dashboard_tools import dashboard_server

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
        model="claude-sonnet-4-20250514",
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


async def run_chat_agent(
    prompt: str,
    session_id: str,
    active_dashboard_uuid: str | None,
    attached_files: list,
) -> str:
    """
    Run the agent for a /chat request with arxiv, api, and dashboard MCP servers,
    all skills enabled, and Write/Bash/Read/Edit built-ins for skill creation and
    in-place dashboard editing. Returns the final text output accumulated from the
    agent loop.
    """
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
        "Before responding to any request, identify which skill applies by checking the description "
        "of available skills. Skills are in .claude/skills/ and are loaded automatically.\n\n"
        "Available skills:\n"
        "- searching-arxiv: find and download papers from arXiv\n"
        "- summarizing-papers: summarize downloaded papers\n"
        "- reading-uploads: locate and read uploaded files by file_id\n"
        "- creating-dashboards: build a single-file HTML dashboard from a document or text\n"
        "- editing-dashboards: edit the active dashboard in place\n"
        "- creating-skills: create new SKILL.md files for databases or APIs the user mentions\n"
        "- plus any database/API skills previously created in .claude/skills/\n\n"
        "When a user wants a dashboard / summary page / overview page from an upload, pasted text, "
        "or downloaded paper, use creating-dashboards. When the user wants to change an existing "
        "dashboard (and an Active dashboard UUID is set below), use editing-dashboards — do NOT "
        "create a new dashboard.\n\n"
        "When a user asks to store, add, index, or retrieve data from a database or external system:\n"
        "  1. Run: ls .claude/skills/ to check for a matching skill.\n"
        "  2. If found, read its SKILL.md and follow its workflow using api:call_api.\n"
        "  3. If not found, use the creating-skills skill to create a SKILL.md for it, "
        "then immediately use the new skill to complete the request.\n\n"
        "When a user provides API details (base URL, endpoints, auth tokens), treat this as a "
        "request to create or update a skill for that system — do it proactively.\n\n"
        "Always complete the user's full request. If the request involves both finding papers AND "
        "ingesting them into a database, do both steps without waiting for a second message.\n\n"
        "----- SESSION CONTEXT -----\n"
        f"{context_block}\n"
        "---------------------------\n"
    )

    options = ClaudeAgentOptions(
        cwd=PROJECT_DIR,
        setting_sources=["user", "project"],
        mcp_servers={"arxiv": arxiv_server, "api": api_server, "dashboard": dashboard_server},
        allowed_tools=[
            "Skill",
            "Read",
            "Write",
            "Edit",
            "Bash",
            "mcp__arxiv__search_arxiv",
            "mcp__arxiv__download_paper",
            "mcp__arxiv__list_downloads",
            "mcp__api__call_api",
            "mcp__dashboard__create_dashboard",
            "mcp__dashboard__extract_text",
        ],
        max_turns=60,
        system_prompt=system_prompt,
        max_buffer_size=AGENT_MAX_BUFFER_SIZE,
    )

    collected_text = []
    turn = 0
    async for message in query(prompt=prompt, options=options):
        turn += 1
        mtype = type(message).__name__
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if hasattr(block, "name"):
                    log.info("Chat agent turn %d: called tool %s", turn, block.name)
                elif hasattr(block, "text") and block.text:
                    collected_text.append(block.text)
                    snippet = block.text[:120].encode("ascii", "replace").decode()
                    log.info("Chat agent turn %d: text — %s", turn, snippet)
        else:
            log.info("Chat agent turn %d: %s", turn, mtype)

    return "\n\n".join(t for t in collected_text if t.strip())


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


@app.route("/chat", methods=["POST"])
def chat():
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

    log.info(
        "Chat message received: %r | history=%d | session=%s | attached=%d | active_dashboard=%s",
        message, len(history), session_id, len(attached_files), active_dashboard_uuid,
    )
    start = time.time()

    try:
        reset_session()

        prompt = build_chat_prompt(history, message)
        agent_text = asyncio.run(
            run_chat_agent(prompt, session_id, active_dashboard_uuid, attached_files)
        )

        session = get_session()
        log.info(
            "Chat agent finished: %d papers found, %d downloaded",
            len(session["papers"]),
            len(session["downloaded"]),
        )

        # Decide what to return:
        # - If the agent produced a dashboard this turn (active UUID changed, OR
        #   agent_text contains a `/d/<uuid>` link), prefer the agent's text so
        #   the dashboard link survives.
        # - Otherwise, if papers were downloaded this session, run the structured
        #   summary path (existing behavior).
        # - Otherwise, use the agent's text output.
        new_active = dashboards_store.get_active(session_id)
        produced_dashboard = (
            (new_active and new_active != active_dashboard_uuid)
            or ("/d/" in agent_text)
        )

        if produced_dashboard and agent_text.strip():
            reply = agent_text
        elif session["papers"] and session["downloaded"]:
            reply = summarize_papers(session, message)
        elif agent_text.strip():
            reply = agent_text
        else:
            error_report = _format_error_report(session)
            reply = error_report if error_report else "I wasn't able to complete that request. Please try again or provide more detail."

        elapsed = time.time() - start
        log.info("Chat pipeline completed in %.2f seconds", elapsed)

        session_files = []
        for fname in session["downloaded"]:
            path = os.path.join(DOWNLOADS_DIR, fname)
            if os.path.exists(path):
                size_kb = os.path.getsize(path) / 1024
                session_files.append({"name": fname, "size_kb": round(size_kb, 1)})

        return jsonify({
            "reply": reply,
            "files": session_files,
            "search_query": session.get("search_query", ""),
            "elapsed_seconds": round(elapsed, 2),
        })

    except Exception as e:
        elapsed = time.time() - start
        log.error("Chat failed after %.2f seconds: %s", elapsed, e)
        session = get_session()
        error_report = _format_error_report(session)
        reply = f"### Request failed\n\n{error_report}" if error_report else f"### Request failed\n\n**Error:** {e}"
        return jsonify({
            "reply": reply,
            "files": [],
            "search_query": session.get("search_query", ""),
            "elapsed_seconds": round(elapsed, 2),
        })


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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
