"""
Custom agent tools for arXiv search and paper download.
Wrapped as an MCP server via the Claude Agent SDK.

Session tracking: each query gets a session dict that accumulates
papers found and downloaded, so the caller can reliably build
summaries after the agent loop finishes.
"""

import logging
import os
import re
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from claude_agent_sdk import tool, create_sdk_mcp_server

log = logging.getLogger(__name__)

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

ARXIV_API_URL = "https://export.arxiv.org/api/query"

# English stop words to strip from search queries — these add noise
# and waste arXiv API query slots without improving relevance.
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "about", "into", "through",
    "during", "before", "after", "above", "below", "between", "under",
    "over", "then", "than", "that", "this", "these", "those", "such",
    "very", "not", "no", "nor", "so", "if", "when", "how", "what",
    "which", "who", "whom", "where", "why", "all", "each", "every",
    "both", "few", "more", "most", "other", "some", "any", "only", "using",
})

# ---------------------------------------------------------------------------
# Session state — reset before each query via reset_session()
# ---------------------------------------------------------------------------
_session: dict = {
    "papers": [],       # papers returned by search (with abstracts)
    "downloaded": [],   # filenames successfully downloaded this query
    "search_query": "", # the actual query string sent to arXiv API
    "errors": [],       # list of error dicts: {"stage": ..., "detail": ...}
}


def reset_session():
    """Clear session state. Call before each new user query."""
    _session["papers"] = []
    _session["downloaded"] = []
    _session["search_query"] = ""
    _session["errors"] = []


def _record_error(stage: str, detail: str):
    """Record an error that occurred during the pipeline."""
    _session["errors"].append({"stage": stage, "detail": detail})
    log.error("Pipeline error [%s]: %s", stage, detail)


def get_session() -> dict:
    """Return a copy of the current session state."""
    return {
        "papers": list(_session["papers"]),
        "downloaded": list(_session["downloaded"]),
        "search_query": _session["search_query"],
        "errors": list(_session["errors"]),
    }


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------
def _parse_arxiv_response(xml_text: str) -> list[dict]:
    """Parse arXiv Atom XML response into a list of paper dicts."""
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(xml_text)
    papers = []

    for entry in root.findall("atom:entry", ns):
        title = entry.find("atom:title", ns)
        summary = entry.find("atom:summary", ns)
        published = entry.find("atom:published", ns)

        authors = []
        for author in entry.findall("atom:author", ns):
            name = author.find("atom:name", ns)
            if name is not None and name.text:
                authors.append(name.text.strip())

        entry_id = entry.find("atom:id", ns)
        arxiv_id = ""
        if entry_id is not None and entry_id.text:
            arxiv_id = entry_id.text.strip().split("/abs/")[-1]

        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
                break

        category_el = entry.find("arxiv:primary_category", ns)
        category = category_el.get("term", "") if category_el is not None else ""

        papers.append({
            "arxiv_id": arxiv_id,
            "title": " ".join(title.text.strip().split()) if title is not None and title.text else "",
            "authors": authors,
            "abstract": " ".join(summary.text.strip().split()) if summary is not None and summary.text else "",
            "published": published.text.strip() if published is not None and published.text else "",
            "pdf_url": pdf_url,
            "category": category,
        })

    return papers


def _fetch_arxiv(url: str, max_retries: int = 4) -> str:
    """Fetch from arXiv API with retry + exponential backoff.

    Catches all failure modes: HTTP 429/5xx, connection errors, read timeouts.
    socket.timeout / TimeoutError are NOT subclasses of URLError on all
    platforms, so they must be caught separately.
    """
    last_err = None
    for attempt in range(max_retries):
        if attempt > 0:
            wait = 5 * (2 ** (attempt - 1))  # 5s, 10s, 20s
            log.info("arXiv retry %d/%d in %ds", attempt + 1, max_retries, wait)
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ArxivSearchAgent/1.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                log.warning("arXiv returned HTTP %d (attempt %d/%d)", e.code, attempt + 1, max_retries)
            else:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            log.warning("arXiv request failed: %s (attempt %d/%d)", e, attempt + 1, max_retries)

    # Record the failure reason in session before raising
    if isinstance(last_err, urllib.error.HTTPError):
        if last_err.code == 429:
            detail = (
                "arXiv API rate limit (HTTP 429). Too many requests were made in a "
                "short period. Please wait a few minutes and try again."
            )
        else:
            detail = f"arXiv API returned HTTP {last_err.code} after {max_retries} retries."
    elif isinstance(last_err, (TimeoutError, OSError)):
        detail = (
            f"arXiv API request timed out after {max_retries} attempts (90s each). "
            "The API may be experiencing high load. Try again shortly."
        )
    else:
        detail = f"arXiv API unreachable after {max_retries} retries: {last_err}"

    _record_error("arXiv Search", detail)
    raise RuntimeError(detail)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@tool(
    "search_arxiv",
    "Search arXiv for scientific articles matching a query. Returns titles, authors, abstracts, and PDF URLs.",
    {
        "query": str,
        "max_results": int,
    },
)
async def search_arxiv(args: dict) -> dict:
    raw_query = args["query"]
    max_results = args.get("max_results", 10)

    # Respect arXiv rate limit: wait 3s before each search to avoid 429s
    time.sleep(3)

    # Build a proper arXiv query: each meaningful term gets an all: prefix
    # joined with AND. Stop words are stripped to reduce noise.
    terms = raw_query.strip().split()
    query_parts = []
    for t in terms:
        if ":" in t:
            query_parts.append(t)  # already prefixed, e.g. au:LeCun, cat:cs.CV
        elif t.lower() not in _STOP_WORDS:
            query_parts.append(f"all:{t}")

    if not query_parts:
        # Fallback: use raw query as-is if everything was stop words
        query_parts = [f"all:{t}" for t in terms]

    search_query = " AND ".join(query_parts)

    log.info("arXiv API query: %s (max_results=%d)", search_query, max_results)
    _session["search_query"] = search_query

    params = urllib.parse.urlencode({
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })

    url = f"{ARXIV_API_URL}?{params}"

    try:
        xml_text = _fetch_arxiv(url)
    except RuntimeError as e:
        # Error already recorded in session by _fetch_arxiv
        return {
            "content": [{"type": "text", "text": f"Search failed: {e}"}]
        }

    papers = _parse_arxiv_response(xml_text)

    # Store in session for later summarization
    _session["papers"] = papers
    log.info("arXiv returned %d paper(s)", len(papers))

    if not papers:
        _record_error("arXiv Search", "The query returned 0 results. Try broader or different search terms.")
        return {
            "content": [{"type": "text", "text": "No results found for the given query."}]
        }

    result_lines = [f"Found {len(papers)} paper(s):\n"]
    for i, p in enumerate(papers, 1):
        result_lines.append(
            f"{i}. **{p['title']}**\n"
            f"   Authors: {', '.join(p['authors'][:5])}"
            f"{'...' if len(p['authors']) > 5 else ''}\n"
            f"   arXiv ID: {p['arxiv_id']}\n"
            f"   Published: {p['published'][:10]}\n"
            f"   Category: {p['category']}\n"
            f"   Abstract: {p['abstract'][:200]}...\n"
            f"   PDF: {p['pdf_url']}\n"
        )

    return {
        "content": [{"type": "text", "text": "\n".join(result_lines)}],
    }


@tool(
    "download_paper",
    "Download a paper PDF from arXiv given its arXiv ID or PDF URL. Saves to the local downloads directory.",
    {
        "arxiv_id": str,
        "pdf_url": str,
    },
)
async def download_paper(args: dict) -> dict:
    arxiv_id = args.get("arxiv_id", "")
    pdf_url = args.get("pdf_url", "")

    if not pdf_url and arxiv_id:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

    if not pdf_url:
        return {
            "content": [{"type": "text", "text": "Error: No arXiv ID or PDF URL provided."}]
        }

    safe_id = re.sub(r"[^\w.\-]", "_", arxiv_id) if arxiv_id else "paper"
    filename = f"{safe_id}.pdf"
    filepath = os.path.join(DOWNLOADS_DIR, filename)

    if os.path.exists(filepath):
        if filename not in _session["downloaded"]:
            _session["downloaded"].append(filename)
        return {
            "content": [{"type": "text", "text": f"Already downloaded: {filename}"}]
        }

    try:
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "ArxivSearchAgent/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(5)
            try:
                req = urllib.request.Request(pdf_url, headers={"User-Agent": "ArxivSearchAgent/1.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
            except Exception as retry_err:
                detail = f"Failed to download {arxiv_id}: HTTP 429 rate limit, retry also failed ({retry_err})"
                _record_error("Paper Download", detail)
                return {"content": [{"type": "text", "text": detail}]}
        else:
            detail = f"Failed to download {arxiv_id}: HTTP {e.code}"
            _record_error("Paper Download", detail)
            return {"content": [{"type": "text", "text": detail}]}
    except (TimeoutError, OSError) as e:
        detail = f"Failed to download {arxiv_id}: request timed out ({e})"
        _record_error("Paper Download", detail)
        return {"content": [{"type": "text", "text": detail}]}

    with open(filepath, "wb") as f:
        f.write(data)

    _session["downloaded"].append(filename)

    size_kb = len(data) / 1024
    return {
        "content": [
            {
                "type": "text",
                "text": f"Downloaded: {filename} ({size_kb:.1f} KB) to {DOWNLOADS_DIR}",
            }
        ]
    }


@tool(
    "list_downloads",
    "List all PDF files currently in the downloads directory with their sizes.",
    {},
)
async def list_downloads(args: dict) -> dict:
    files = []
    for f in sorted(os.listdir(DOWNLOADS_DIR)):
        if f.lower().endswith(".pdf"):
            path = os.path.join(DOWNLOADS_DIR, f)
            size_kb = os.path.getsize(path) / 1024
            files.append(f"{f} ({size_kb:.1f} KB)")

    if not files:
        return {
            "content": [{"type": "text", "text": "No PDF files in the downloads directory."}]
        }

    text = f"Downloaded files ({len(files)}):\n" + "\n".join(f"  - {f}" for f in files)
    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
arxiv_server = create_sdk_mcp_server(
    name="arxiv",
    version="1.0.0",
    tools=[search_arxiv, download_paper, list_downloads],
)
