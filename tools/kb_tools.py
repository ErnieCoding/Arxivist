"""
Knowledge Base MCP tools — typed, deterministic wrappers around the
ArangoDB-backed knowledge graph at neo.rndl.ru:5001.

Auth: X-API-Key header (from KNOWLEDGE_BASE_API_KEY env var).
All async endpoints (qa/ask, expand, create) are polled to completion
inside the tool — the agent receives the final result, not a task_id.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

from claude_agent_sdk import tool, create_sdk_mcp_server

log = logging.getLogger(__name__)

KB_BASE_URL = os.environ.get("KNOWLEDGE_BASE_URL", "http://neo.rndl.ru:5001").rstrip("/")
KB_API_KEY = os.environ.get("KNOWLEDGE_BASE_API_KEY", "")

# Poll interval and max wait for async tasks
_POLL_INTERVAL = 2.0
_POLL_TIMEOUT = 120


def _headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if KB_API_KEY:
        h["X-API-Key"] = KB_API_KEY
    return h


def _request(method: str, path: str, body=None, timeout: int = 30) -> dict:
    """Make a request to the KB API. Returns {status, data, error}."""
    url = f"{KB_BASE_URL}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return {"status": resp.status, "data": json.loads(raw), "error": None}
            except json.JSONDecodeError:
                return {"status": resp.status, "data": raw.decode("utf-8", errors="replace"), "error": None}
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:600]
        except Exception:
            pass
        return {"status": e.code, "data": None, "error": f"HTTP {e.code}: {err_body}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"status": 0, "data": None, "error": f"Connection error: {e}"}


def _poll_task(task_id: str, description: str = "task") -> dict:
    """
    Poll /api/tasks/<task_id> until state is SUCCESS or FAILURE.
    Returns {"ok": True/False, "result": ..., "error": ...}.
    """
    deadline = time.monotonic() + _POLL_TIMEOUT
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        r = _request("GET", f"/api/tasks/{task_id}", timeout=15)
        if r["error"]:
            return {"ok": False, "error": f"Poll failed: {r['error']}"}

        d = r["data"]
        state = (d.get("state") or "").upper()
        log.debug("KB %s task_id=%s state=%s attempt=%d", description, task_id, state, attempt)

        if state in ("SUCCESS", "DONE", "COMPLETED"):
            return {"ok": True, "result": d.get("result"), "error": None}
        if state in ("FAILURE", "FAILED", "ERROR"):
            return {"ok": False, "error": d.get("error") or f"Task failed (state={state})"}

        # STARTED / PENDING / PROGRESS — keep polling
        time.sleep(_POLL_INTERVAL)

    return {"ok": False, "error": f"Timed out after {_POLL_TIMEOUT}s waiting for {description} task_id={task_id}"}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(
    "list_kb_databases",
    "List all databases available in the knowledge base.",
    {},
)
async def list_kb_databases(args: dict) -> dict:
    result = _request("GET", "/api/databases")
    if result["error"]:
        return {"content": [{"type": "text", "text": f"Error: {result['error']}"}]}

    data = result["data"]
    # Response shape: {"databases": [{name, status, type, ...}, ...]}
    dbs = data.get("databases", data) if isinstance(data, dict) else data
    if not isinstance(dbs, list):
        dbs = [dbs]

    if not dbs:
        return {"content": [{"type": "text", "text": "No databases found."}]}

    lines = [f"Found {len(dbs)} database(s):"]
    for db in dbs:
        if isinstance(db, dict):
            lines.append(f"  - {db.get('name')} | status={db.get('status')} | type={db.get('type')}")
        else:
            lines.append(f"  - {db}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "query_knowledge_base",
    (
        "Ask a natural language question against a knowledge graph database. "
        "ALWAYS call this FIRST when the user asks about a company, person, or topic — "
        "before searching the web — to check what is already known. "
        "Waits for the answer automatically (async task polling is built in)."
    ),
    {
        "type": "object",
        "properties": {
            "database_name": {
                "type": "string",
                "description": "Database name. Use 'companies' for company intelligence. Call list_kb_databases to discover available databases.",
            },
            "question": {
                "type": "string",
                "description": "Natural language question. Be specific: include company name, topic, time period.",
            },
            "user_context": {
                "type": "string",
                "description": "Optional context about why the question is being asked.",
            },
        },
        "required": ["database_name", "question"],
    },
)
async def query_knowledge_base(args: dict) -> dict:
    db = args["database_name"]
    question = args["question"]
    ctx = args.get("user_context", "")

    payload: dict = {"database_name": db, "question": question}
    if ctx:
        payload["user_context"] = ctx

    log.info("KB query  db=%s  q=%r", db, question[:120])
    result = _request("POST", "/api/qa/ask", body=payload, timeout=30)

    if result["error"]:
        return {"content": [{"type": "text", "text": f"KB query failed: {result['error']}"}]}

    data = result["data"]

    # If the response already contains the answer (sync response)
    if isinstance(data, dict) and data.get("answer"):
        answer = data["answer"]
        sources = data.get("sources") or []
        text = f"Knowledge base answer:\n{answer}"
        if sources:
            text += f"\n\nSources: {', '.join(str(s) for s in sources[:5])}"
        return {"content": [{"type": "text", "text": text}]}

    # Async: response has task_id — poll for result
    task_id = data.get("task_id") if isinstance(data, dict) else None
    if not task_id:
        return {"content": [{"type": "text", "text": f"Unexpected KB response: {json.dumps(data)[:300]}"}]}

    log.info("KB query async  task_id=%s  polling…", task_id)
    poll = _poll_task(task_id, description="qa/ask")

    if not poll["ok"]:
        return {"content": [{"type": "text", "text": f"KB query task failed: {poll['error']}"}]}

    result_data = poll["result"] or {}
    if isinstance(result_data, str):
        answer = result_data
        sources = []
    else:
        answer = (
            result_data.get("answer") or result_data.get("response") or result_data.get("text")
            or json.dumps(result_data, ensure_ascii=False)
        )
        sources = result_data.get("sources") or []

    text = f"Knowledge base answer:\n{answer}"
    if sources:
        text += f"\n\nSources: {', '.join(str(s) for s in sources[:5])}"
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "create_kb_database",
    "Create a new knowledge graph database. Call this if expand_knowledge_base reports the database does not exist.",
    {
        "type": "object",
        "properties": {
            "database_name": {
                "type": "string",
                "description": "Unique database name (lowercase, no spaces). Example: 'companies'.",
            },
        },
        "required": ["database_name"],
    },
)
async def create_kb_database(args: dict) -> dict:
    db = args["database_name"]
    log.info("KB create database: %s", db)

    result = _request("POST", "/api/database/create", body={"database_name": db, "sync": True}, timeout=30)
    if result["error"]:
        return {"content": [{"type": "text", "text": f"Failed to create '{db}': {result['error']}"}]}

    data = result["data"]
    task_id = data.get("task_id") if isinstance(data, dict) else None

    if task_id:
        poll = _poll_task(task_id, description="create_database")
        if not poll["ok"]:
            return {"content": [{"type": "text", "text": f"Create task failed: {poll['error']}"}]}

    return {"content": [{"type": "text", "text": f"Database '{db}' created successfully."}]}


@tool(
    "expand_knowledge_base",
    (
        "Add text chunks to an existing knowledge graph database. "
        "Use this to persist company profiles, news, HR data, or any gathered intelligence. "
        "Each chunk should be 300–800 tokens and self-contained. "
        "If the database does not exist, call create_kb_database first. "
        "Waits for completion automatically (async polling built in)."
    ),
    {
        "type": "object",
        "properties": {
            "database_name": {
                "type": "string",
                "description": "Target database. Use 'companies' for company intelligence.",
            },
            "texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of text chunks (300–800 tokens each, self-contained factual paragraphs).",
            },
            "metadata": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "One metadata dict per chunk. Recommended fields: "
                    "type (company_profile|hr_data|news|vacancy|financial), "
                    "source (URL or 'web search'), company (company name), date (YYYY-MM-DD)."
                ),
            },
        },
        "required": ["database_name", "texts"],
    },
)
async def expand_knowledge_base(args: dict) -> dict:
    db = args["database_name"]
    texts: list = args.get("texts") or []
    metadata: list = args.get("metadata") or []

    if not texts:
        return {"content": [{"type": "text", "text": "Error: texts list is empty."}]}

    while len(metadata) < len(texts):
        metadata.append({})

    log.info("KB expand  db=%s  chunks=%d", db, len(texts))
    payload = {"database_name": db, "texts": texts, "metadata": metadata, "sync": True}
    result = _request("POST", "/api/databases/expand", body=payload, timeout=60)

    if result["error"]:
        if result["status"] == 404:
            return {"content": [{"type": "text", "text": f"Database '{db}' not found. Call create_kb_database('{db}') first."}]}
        return {"content": [{"type": "text", "text": f"KB expand failed: {result['error']}"}]}

    data = result["data"]
    task_id = data.get("task_id") if isinstance(data, dict) else None

    if task_id:
        poll = _poll_task(task_id, description="expand")
        if not poll["ok"]:
            return {"content": [{"type": "text", "text": f"Expand task failed: {poll['error']}"}]}

    return {"content": [{"type": "text", "text": f"Successfully added {len(texts)} chunk(s) to database '{db}'."}]}


@tool(
    "get_kb_task_status",
    "Check the status of a knowledge base background task by task_id.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to check."},
        },
        "required": ["task_id"],
    },
)
async def get_kb_task_status(args: dict) -> dict:
    task_id = args["task_id"]
    result = _request("GET", f"/api/tasks/{task_id}", timeout=15)
    if result["error"]:
        return {"content": [{"type": "text", "text": f"Failed: {result['error']}"}]}

    data = result["data"]
    if isinstance(data, dict):
        state = data.get("state", "unknown")
        progress = data.get("progress", 0)
        parts = [f"Task {task_id}: state={state}, progress={progress}%"]
        if data.get("result"):
            parts.append(f"Result: {json.dumps(data['result'], ensure_ascii=False)[:500]}")
        if data.get("error"):
            parts.append(f"Error: {data['error']}")
        text = "\n".join(parts)
    else:
        text = str(data)

    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

kb_server = create_sdk_mcp_server(
    name="kb",
    version="1.0.0",
    tools=[list_kb_databases, query_knowledge_base, create_kb_database, expand_knowledge_base, get_kb_task_status],
)
