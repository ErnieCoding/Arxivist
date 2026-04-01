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
from flask import Flask, render_template, request, jsonify

load_dotenv()

from claude_agent_sdk import query, ClaudeAgentOptions
from tools import arxiv_server, DOWNLOADS_DIR, reset_session, get_session

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


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
        setting_sources=["project"],
        mcp_servers={"arxiv": arxiv_server},
        allowed_tools=[
            "Skill",
            "mcp__arxiv__search_arxiv",
            "mcp__arxiv__download_paper",
            "mcp__arxiv__list_downloads",
        ],
        max_turns=20,
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
    return render_template("index.html")


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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
