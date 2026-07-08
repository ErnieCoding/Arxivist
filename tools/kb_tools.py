"""
Knowledge Base MCP tools — typed, deterministic wrappers around the
ArangoDB-backed knowledge graph at neo.rndl.ru:5001.

Auth: X-API-Key header (from KNOWLEDGE_BASE_API_KEY env var).

Ingestion model (verified against the live server):
  - Data is ingested as JSON documents (one file per document).
  - The server maps JSON into the graph via LLM schema detection.
  - Raw prose is NOT accepted on the JSON fast-path — always send structured JSON.
  - NEW database:      POST /api/server/files/upload  →  POST /api/server/database/create-from-files
  - EXISTING database: POST /api/server/database/add-file   (uploads + expands in one call)
  - Task status:       GET  /api/server/tasks/{task_id}
  - Query (QA):        POST /api/qa/ask  →  GET /api/tasks/{task_id}

All async operations are polled to completion inside the tool, so the agent
receives the final result rather than a task_id.
"""

import base64
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

_POLL_INTERVAL = 3.0
_POLL_TIMEOUT = 180


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


def _poll_task(task_id: str, task_path: str, description: str = "task") -> dict:
    """
    Poll a task endpoint until state is SUCCESS or FAILURE.
    task_path is the base path, e.g. '/api/server/tasks' or '/api/tasks'.
    Returns {"ok": bool, "result": ..., "error": ...}.
    """
    deadline = time.monotonic() + _POLL_TIMEOUT
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        r = _request("GET", f"{task_path}/{task_id}", timeout=15)
        if r["error"]:
            return {"ok": False, "result": None, "error": f"Poll failed: {r['error']}"}

        d = r["data"] if isinstance(r["data"], dict) else {}
        state = (d.get("state") or "").upper()
        log.debug("KB %s task=%s state=%s attempt=%d", description, task_id, state, attempt)

        if state in ("SUCCESS", "DONE", "COMPLETED"):
            return {"ok": True, "result": d.get("result"), "error": None}
        if state in ("FAILURE", "FAILED", "ERROR"):
            return {"ok": False, "result": d.get("result"), "error": d.get("error") or f"Task failed (state={state})"}

        time.sleep(_POLL_INTERVAL)

    return {"ok": False, "result": None, "error": f"Timed out after {_POLL_TIMEOUT}s waiting for {description}"}


def _database_names() -> list[str]:
    """Return the list of existing database names, or [] on error."""
    r = _request("GET", "/api/databases")
    if r["error"] or not isinstance(r["data"], dict):
        return []
    out = []
    for db in r["data"].get("databases", []):
        if isinstance(db, dict) and db.get("name"):
            out.append(db["name"])
        elif isinstance(db, str):
            out.append(db)
    return out


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
        "The answer is returned directly (async task polling is handled internally)."
    ),
    {
        "type": "object",
        "properties": {
            "database_name": {
                "type": "string",
                "description": "Database to query. Use 'companies' for company intelligence. Call list_kb_databases to discover others.",
            },
            "question": {
                "type": "string",
                "description": "Natural language question. Be specific: include company name, topic, time period.",
            },
            "user_context": {
                "type": "string",
                "description": "Optional extra context about why the question is being asked.",
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

    data = result["data"] if isinstance(result["data"], dict) else {}

    # Sync answer path
    if data.get("answer"):
        return {"content": [{"type": "text", "text": f"Knowledge base answer:\n{data['answer']}"}]}

    # Async path — poll /api/tasks/
    task_id = data.get("task_id")
    if not task_id:
        return {"content": [{"type": "text", "text": f"Unexpected KB response: {json.dumps(data, ensure_ascii=False)[:300]}"}]}

    log.info("KB query async task=%s polling…", task_id)
    poll = _poll_task(task_id, "/api/tasks", description="qa/ask")
    if not poll["ok"]:
        return {"content": [{"type": "text", "text": f"KB query task failed: {poll['error']}"}]}

    res = poll["result"] or {}
    if isinstance(res, str):
        answer = res
    else:
        answer = res.get("answer") or res.get("response") or res.get("text") or json.dumps(res, ensure_ascii=False)

    return {"content": [{"type": "text", "text": f"Knowledge base answer:\n{answer}"}]}


@tool(
    "add_document_to_kb",
    (
        "Persist a structured JSON document into a knowledge graph database. "
        "This is the ONLY supported ingestion path — the KB maps JSON into the graph. "
        "Pass a `document` object (NOT raw prose): structure the gathered intelligence into fields "
        "like company, industry, description, ai_strategy, recent_news, vacancies, competitors, sources. "
        "Automatically creates the database if it doesn't exist, or appends to it if it does. "
        "Waits for full processing before returning."
    ),
    {
        "type": "object",
        "properties": {
            "database_name": {
                "type": "string",
                "description": "Target database. Use 'companies' for company intelligence.",
            },
            "document": {
                "type": "object",
                "description": (
                    "The knowledge as a JSON object. Use consistent field names across documents in the "
                    "same database so the graph schema stays stable. For a company, include at least: "
                    "company (name), and any of: aliases, industry, founded, headquarters, website, employees, "
                    "description, focus_area, hr_and_people, recent_news (list), vacancies (object), "
                    "competitors (list), financials, sources (list), gathered_at (YYYY-MM-DD)."
                ),
            },
            "filename": {
                "type": "string",
                "description": "Filename for this document, ending in .json. Use a slug of the entity, e.g. 'yandex.json'.",
            },
        },
        "required": ["database_name", "document", "filename"],
    },
)
async def add_document_to_kb(args: dict) -> dict:
    db = args["database_name"]
    document = args.get("document")
    filename = args.get("filename") or "document.json"

    if not isinstance(document, dict) or not document:
        return {"content": [{"type": "text", "text": "Error: 'document' must be a non-empty JSON object."}]}
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    # Serialize the document to JSON bytes and base64-encode.
    doc_bytes = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    b64 = base64.b64encode(doc_bytes).decode("ascii")

    exists = db in _database_names()
    log.info("KB add_document  db=%s  file=%s  exists=%s", db, filename, exists)

    if exists:
        # Append to existing DB — add-file uploads + expands in one call.
        body = {"database_name": db, "filename": filename, "file_type": "json", "file_content": b64}
        r = _request("POST", "/api/server/database/add-file", body=body, timeout=60)
        if r["error"]:
            return {"content": [{"type": "text", "text": f"add-file failed: {r['error']}"}]}
        task_id = r["data"].get("task_id") if isinstance(r["data"], dict) else None
        if not task_id:
            return {"content": [{"type": "text", "text": f"add-file: unexpected response {json.dumps(r['data'], ensure_ascii=False)[:300]}"}]}
        poll = _poll_task(task_id, "/api/server/tasks", description="add-file")
        if not poll["ok"]:
            return {"content": [{"type": "text", "text": f"Ingestion task failed: {poll['error']}"}]}
        res = poll["result"] or {}
        processed = res.get("files_processed", "?") if isinstance(res, dict) else "?"
        rejected = res.get("rejected_files_count", 0) if isinstance(res, dict) else 0
        note = ""
        if rejected:
            note = f" ⚠ {rejected} file(s) rejected: {json.dumps(res.get('rejected_files'), ensure_ascii=False)[:200]}"
        return {"content": [{"type": "text", "text": f"Added '{filename}' to database '{db}' (files_processed={processed}).{note}"}]}

    # New DB — upload the file, then create-from-files.
    up_body = {"database_name": db, "filename": filename, "file_type": "json", "file_content": b64}
    up = _request("POST", "/api/server/files/upload", body=up_body, timeout=60)
    if up["error"]:
        return {"content": [{"type": "text", "text": f"File upload failed: {up['error']}"}]}

    cr = _request("POST", "/api/server/database/create-from-files", body={"database_name": db}, timeout=60)
    if cr["error"]:
        return {"content": [{"type": "text", "text": f"create-from-files failed: {cr['error']}"}]}
    task_id = cr["data"].get("task_id") if isinstance(cr["data"], dict) else None
    if not task_id:
        return {"content": [{"type": "text", "text": f"create-from-files: unexpected response {json.dumps(cr['data'], ensure_ascii=False)[:300]}"}]}

    poll = _poll_task(task_id, "/api/server/tasks", description="create-from-files")
    if not poll["ok"]:
        return {"content": [{"type": "text", "text": f"Database creation task failed: {poll['error']}"}]}

    return {"content": [{"type": "text", "text": f"Created database '{db}' and ingested '{filename}'."}]}


@tool(
    "get_kb_task_status",
    "Check the status of a knowledge base background task by task_id (server file/DB operations).",
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
    # Server ops live under /api/server/tasks; fall back to /api/tasks for QA tasks.
    result = _request("GET", f"/api/server/tasks/{task_id}", timeout=15)
    if result["status"] == 404:
        result = _request("GET", f"/api/tasks/{task_id}", timeout=15)
    if result["error"]:
        return {"content": [{"type": "text", "text": f"Failed: {result['error']}"}]}

    data = result["data"] if isinstance(result["data"], dict) else {}
    state = data.get("state", "unknown")
    progress = data.get("progress", 0)
    parts = [f"Task {task_id}: state={state}, progress={progress}%"]
    if data.get("result"):
        parts.append(f"Result: {json.dumps(data['result'], ensure_ascii=False)[:500]}")
    if data.get("error"):
        parts.append(f"Error: {data['error']}")
    return {"content": [{"type": "text", "text": "\n".join(parts)}]}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

kb_server = create_sdk_mcp_server(
    name="kb",
    version="2.0.0",
    tools=[list_kb_databases, query_knowledge_base, add_document_to_kb, get_kb_task_status],
)
