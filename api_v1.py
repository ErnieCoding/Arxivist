"""
/api/v1 — the API-key-gated backend module for external consumers.

Two endpoint families (see docs/openapi.yaml for the full contract):

  Agent (asynchronous jobs — an LLM run takes minutes):
      POST   /api/v1/agent/jobs        → 202 {job_id}   (runs in a dedicated
      GET    /api/v1/agent/jobs/<id>   → status/progress/result    process,
      DELETE /api/v1/agent/jobs/<id>   → cancel          never holds a worker)

  Typed (deterministic, synchronous, no LLM):
      /api/v1/hh/*           — vacancies, employers, resumes, reference data
      /api/v1/kb/*           — knowledge-base query / ingest / task status
      /api/v1/candidates/save — resume → structured doc → 'candidates' DB
      /api/v1/arxiv/search   — structured paper search + summaries
      /api/v1/files          — upload documents / download fetched PDFs
      /api/v1/dashboards     — list generated dashboards

Auth: every route (except /docs, /openapi.yaml, /health) requires a valid
key from config/api_keys.json in the `X-API-Key` header (or
`Authorization: Bearer <key>`). Keys are managed with scripts/apikeys.py.

Error envelope (non-2xx): {"error": {"code": "<machine_code>", "message": "…"}}
Status codes: 401 invalid/missing API key · 403 HH user authorization missing
(the key is fine; the HeadHunter account isn't signed in) · 429 job capacity.

The browser debug UI (routes outside /api/v1) is intentionally NOT key-gated.
"""

import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import date

from flask import Blueprint, g, jsonify, request, send_file

import agent_pipeline as pipeline
import jobs_store
import uploads_store
import dashboards_store
from arxiv_tools import DOWNLOADS_DIR
from tools import api_keys, hh_auth
from tools import hh_tools
from tools import kb_tools

log = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OPENAPI_PATH = os.path.join(PROJECT_DIR, "docs", "openapi.yaml")

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

# Paths (relative to the blueprint prefix) that need no API key.
_OPEN_SUFFIXES = ("/docs", "/openapi.yaml", "/health")

MAX_CONCURRENT_AGENT_JOBS = int(os.environ.get("MAX_CONCURRENT_AGENT_JOBS", "2"))
# Comma-separated origins, or "*". Empty (default) = no CORS headers
# (server-to-server integration).
API_CORS_ORIGINS = os.environ.get("API_CORS_ORIGINS", "").strip()

_PDF_NAME_RE = re.compile(r"^[\w.\-]+\.pdf$")


# ---------------------------------------------------------------------------
# Envelope helpers, auth hook, CORS
# ---------------------------------------------------------------------------

def _err(status: int, code: str, message: str, **extra):
    body = {"error": {"code": code, "message": message, **extra}}
    return jsonify(body), status


def _presented_key() -> str:
    key = request.headers.get("X-API-Key", "")
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[len("Bearer "):].strip()
    return key


@api_v1_bp.before_request
def _require_api_key():
    if request.method == "OPTIONS":  # CORS preflight carries no credentials
        return _cors_preflight()
    if request.path.endswith(_OPEN_SUFFIXES):
        return None
    record = api_keys.verify_key(_presented_key())
    if record is None:
        return _err(401, "invalid_api_key",
                    "Provide a valid key in the X-API-Key header. "
                    "Keys are issued with scripts/apikeys.py.")
    g.api_key = record
    log.info("API request: %s %s [key=%s %r]",
             request.method, request.path, record["key_id"], record.get("label"))
    return None


def _cors_preflight():
    if not API_CORS_ORIGINS:
        return _err(405, "cors_disabled", "CORS is not enabled on this server.")
    resp = jsonify({})
    resp.status_code = 204
    return resp


@api_v1_bp.after_request
def _cors_headers(resp):
    if API_CORS_ORIGINS:
        origin = request.headers.get("Origin", "")
        allowed = ("*" if API_CORS_ORIGINS == "*"
                   else origin if origin in [o.strip() for o in API_CORS_ORIGINS.split(",")]
                   else "")
        if allowed:
            resp.headers["Access-Control-Allow-Origin"] = allowed
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


def _hh_authorize_url() -> str:
    base = os.environ.get("APP_BASE", "").rstrip("/")
    return f"{base}/hh/authorize"


def _has_hh_user_token() -> bool:
    tok = hh_auth._read()
    return (bool(tok.get("refresh_token"))
            or tok.get("grant") == "authorization_code"
            or bool(os.environ.get("HH_ACCESS_TOKEN")))


def _hh_auth_required_response():
    return _err(
        403, "hh_user_authorization_required",
        "Resume access needs a one-time HeadHunter employer sign-in. Open the "
        "authorize URL in a browser, complete the login, then retry.",
        authorize_url=_hh_authorize_url(),
    )


def _hh_passthrough(path: str, params: dict):
    """Call the HH API via the shared helper and map errors to the envelope."""
    result = hh_tools._get(path, params=params or None)
    if result["ok"]:
        return jsonify(result["data"])
    err = str(result.get("error", ""))
    if "403" in err:
        if path.startswith("/resumes"):
            return _hh_auth_required_response()
        return _err(502, "hh_forbidden",
                    "HeadHunter rejected the request. Check HH_CLIENT_ID / "
                    "HH_CLIENT_SECRET configuration.", detail=err[:200])
    return _err(502, "hh_error", "HeadHunter request failed.", detail=err[:300])


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@api_v1_bp.get("/health")
def health():
    return jsonify({"status": "ok", "time": int(time.time())})


@api_v1_bp.get("/openapi.yaml")
def openapi_spec():
    return send_file(OPENAPI_PATH, mimetype="text/yaml")


@api_v1_bp.get("/docs")
def swagger_ui():
    # Same inline-HTML pattern as app.py's _hh_page; swagger-ui from CDN,
    # spec URL is relative so it works behind the nginx sub-path too.
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Arxivist API — Swagger UI</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
<style>body { margin:0; } .topbar { display:none; }</style></head>
<body><div id="ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
  SwaggerUIBundle({ url: 'openapi.yaml', dom_id: '#ui', deepLinking: true,
                    persistAuthorization: true });
</script></body></html>"""


# ---------------------------------------------------------------------------
# Agent jobs (202 + poll)
# ---------------------------------------------------------------------------

@api_v1_bp.post("/agent/jobs")
def create_agent_job():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return _err(400, "invalid_request", "'message' is required.")

    history = data.get("history") or []
    if not isinstance(history, list):
        return _err(400, "invalid_request", "'history' must be a list of {role, content}.")
    session_id = (data.get("session_id") or "").strip() or f"api-{uuid.uuid4().hex}"
    attached_file_ids = data.get("attached_file_ids") or []

    if jobs_store.active_count() >= MAX_CONCURRENT_AGENT_JOBS:
        resp, code = _err(429, "too_many_jobs",
                          f"Agent job capacity ({MAX_CONCURRENT_AGENT_JOBS}) is busy. "
                          "Retry after the current jobs finish.")
        resp.headers["Retry-After"] = "30"
        return resp, code

    job_id = jobs_store.create_job({
        "message": message,
        "history": history,
        "session_id": session_id,
        "attached_file_ids": attached_file_ids,
        "api_key_id": g.api_key["key_id"],
    })

    runner_log = open(os.path.join(jobs_store.JOBS_DIR, job_id, "runner.log"), "ab")
    subprocess.Popen(
        [sys.executable, os.path.join(PROJECT_DIR, "job_runner.py"), job_id],
        cwd=PROJECT_DIR,
        stdout=runner_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runner_log.close()

    log.info("Agent job %s submitted (session=%s)", job_id, session_id)
    return jsonify({
        "job_id": job_id,
        "state": "queued",
        "session_id": session_id,
        "poll": f"/api/v1/agent/jobs/{job_id}",
    }), 202


@api_v1_bp.get("/agent/jobs/<job_id>")
def get_agent_job(job_id):
    status = jobs_store.read_status(job_id)
    if status is None:
        return _err(404, "job_not_found", f"No job with id {job_id}.")
    body = {k: v for k, v in status.items() if k != "pid"}
    if status.get("state") == "running":
        events = jobs_store.read_events(job_id, limit=20)
        body["progress"] = [e.get("phrase", "") for e in events]
    return jsonify(body)


@api_v1_bp.delete("/agent/jobs/<job_id>")
def cancel_agent_job(job_id):
    status = jobs_store.cancel(job_id)
    if status is None:
        return _err(404, "job_not_found", f"No job with id {job_id}.")
    return jsonify({k: v for k, v in status.items() if k != "pid"})


# ---------------------------------------------------------------------------
# Files (uploads for vacancy-doc-driven jobs; downloads of fetched PDFs)
# ---------------------------------------------------------------------------

@api_v1_bp.post("/files")
def api_upload():
    if "file" not in request.files:
        return _err(400, "invalid_request", "Multipart field 'file' is required.")
    saved, errors = [], []
    for fs in request.files.getlist("file"):
        if not fs or not fs.filename:
            errors.append({"name": "?", "detail": "empty filename"})
            continue
        try:
            saved.append(uploads_store.save_upload(fs))
        except uploads_store.UploadError as e:
            errors.append({"name": fs.filename, "detail": str(e)})
        except Exception as e:
            log.exception("API upload failed for %s", fs.filename)
            errors.append({"name": fs.filename, "detail": f"Upload failed: {e}"})
    return jsonify({"files": saved, "errors": errors})


@api_v1_bp.get("/files/<path:name>")
def api_download_pdf(name):
    if not _PDF_NAME_RE.match(name):
        return _err(400, "invalid_request", "Only plain PDF filenames are allowed.")
    path = os.path.join(DOWNLOADS_DIR, name)
    if not os.path.isfile(path):
        return _err(404, "file_not_found", f"No downloaded file named {name}.")
    return send_file(path, mimetype="application/pdf")


# ---------------------------------------------------------------------------
# HeadHunter — typed passthrough (raw api.hh.ru JSON)
# ---------------------------------------------------------------------------

_VACANCY_PARAMS = ("text", "area", "employer_id", "experience", "employment",
                   "schedule", "salary", "order_by", "per_page", "page", "date_from")
_RESUME_PARAMS = ("text", "area", "experience", "salary_from", "salary_to", "currency",
                  "schedule", "employment", "education_level", "age_from", "age_to",
                  "relocation", "job_search_status", "period", "label", "order_by",
                  "per_page", "page")


def _collect_params(allowed) -> dict:
    return {k: request.args[k] for k in allowed if request.args.get(k) not in (None, "")}


@api_v1_bp.get("/hh/vacancies")
def hh_vacancies():
    return _hh_passthrough("/vacancies", _collect_params(_VACANCY_PARAMS))


@api_v1_bp.get("/hh/vacancies/<vacancy_id>")
def hh_vacancy(vacancy_id):
    return _hh_passthrough(f"/vacancies/{vacancy_id}", {})


@api_v1_bp.get("/hh/employers")
def hh_employers():
    params = _collect_params(("text", "area", "type", "per_page", "page"))
    return _hh_passthrough("/employers", params)


@api_v1_bp.get("/hh/employers/<employer_id>")
def hh_employer(employer_id):
    result = hh_tools._get(f"/employers/{employer_id}")
    if not result["ok"]:
        return _hh_passthrough(f"/employers/{employer_id}", {})  # reuse error mapping
    data = result["data"]
    if request.args.get("include_vacancies") == "true":
        vac = hh_tools._get("/vacancies", params={"employer_id": employer_id, "per_page": 20})
        if vac["ok"]:
            data["vacancies"] = vac["data"].get("items", [])
    return jsonify(data)


@api_v1_bp.get("/hh/resumes")
def hh_resumes():
    if not _has_hh_user_token():
        return _hh_auth_required_response()
    params = _collect_params(_RESUME_PARAMS)
    # Same deterministic guards as the MCP tool (verified live vs api.hh.ru):
    if ("salary_from" in params or "salary_to" in params) and "currency" not in params:
        params["currency"] = "RUR"
    if "relocation" in params and "area" not in params:
        return _err(400, "invalid_request",
                    "'relocation' requires 'area' to be set (HeadHunter constraint).")
    return _hh_passthrough("/resumes", params)


@api_v1_bp.get("/hh/resumes/<resume_id>")
def hh_resume(resume_id):
    if not _has_hh_user_token():
        return _hh_auth_required_response()
    return _hh_passthrough(f"/resumes/{resume_id}", {})


@api_v1_bp.get("/hh/reference/<ref_type>")
def hh_reference(ref_type):
    paths = {"areas": "/areas", "professional_roles": "/professional_roles",
             "dictionaries": "/dictionaries", "skills": "/skills"}
    if ref_type not in paths:
        return _err(400, "invalid_request",
                    f"Unknown reference type '{ref_type}'. One of: {', '.join(paths)}.")
    params = {}
    if ref_type == "skills":
        q = request.args.get("query", "")
        if not q:
            return _err(400, "invalid_request", "'query' parameter is required for skills.")
        params["text"] = q
    return _hh_passthrough(paths[ref_type], params)


@api_v1_bp.get("/hh/auth/status")
def hh_auth_status():
    status = hh_auth.token_status()
    status["user_authorized"] = _has_hh_user_token()
    status["authorize_url"] = _hh_authorize_url()
    return jsonify(status)


# ---------------------------------------------------------------------------
# Knowledge base — typed wrappers over the same helpers the MCP tools use
# ---------------------------------------------------------------------------

@api_v1_bp.get("/kb/databases")
def kb_databases():
    result = kb_tools._request("GET", "/api/databases")
    if result["error"]:
        return _err(502, "kb_error", "Knowledge base request failed.", detail=result["error"][:300])
    return jsonify(result["data"])


@api_v1_bp.post("/kb/query")
def kb_query():
    data = request.get_json(silent=True) or {}
    db = (data.get("database_name") or "").strip()
    question = (data.get("question") or "").strip()
    if not db or not question:
        return _err(400, "invalid_request", "'database_name' and 'question' are required.")

    payload = {"database_name": db, "question": question}
    if data.get("user_context"):
        payload["user_context"] = data["user_context"]

    result = kb_tools._request("POST", "/api/qa/ask", body=payload, timeout=30)
    if result["error"]:
        return _err(502, "kb_error", "Knowledge base query failed.", detail=result["error"][:300])

    body = result["data"] if isinstance(result["data"], dict) else {}
    if body.get("answer"):
        return jsonify({"answer": body["answer"], "database_name": db})

    task_id = body.get("task_id")
    if not task_id:
        return _err(502, "kb_error", "Unexpected knowledge base response.",
                    detail=json.dumps(body, ensure_ascii=False)[:300])

    poll = kb_tools._poll_task(task_id, "/api/tasks", description="qa/ask")
    if not poll["ok"]:
        return _err(502, "kb_error", "Knowledge base query task failed.", detail=str(poll["error"])[:300])

    res = poll["result"] or {}
    if isinstance(res, str):
        answer = res
    else:
        answer = res.get("answer") or res.get("response") or res.get("text") or json.dumps(res, ensure_ascii=False)
    return jsonify({"answer": answer, "database_name": db})


def _kb_ingest(db: str, document: dict, filename: str, wait: bool):
    """Shared JSON-document ingestion (used by /kb/documents and /candidates/save)."""
    if not filename.lower().endswith(".json"):
        filename += ".json"
    b64 = base64.b64encode(
        json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")

    exists = db in kb_tools._database_names()
    if exists:
        body = {"database_name": db, "filename": filename, "file_type": "json", "file_content": b64}
        r = kb_tools._request("POST", "/api/server/database/add-file", body=body, timeout=60)
        if r["error"]:
            return {"ok": False, "error": r["error"]}
        task_id = r["data"].get("task_id") if isinstance(r["data"], dict) else None
    else:
        up = kb_tools._request("POST", "/api/server/files/upload",
                               body={"database_name": db, "filename": filename,
                                     "file_type": "json", "file_content": b64}, timeout=60)
        if up["error"]:
            return {"ok": False, "error": up["error"]}
        cr = kb_tools._request("POST", "/api/server/database/create-from-files",
                               body={"database_name": db}, timeout=60)
        if cr["error"]:
            return {"ok": False, "error": cr["error"]}
        task_id = cr["data"].get("task_id") if isinstance(cr["data"], dict) else None

    if not task_id:
        return {"ok": False, "error": "knowledge base returned no task_id"}
    if not wait:
        return {"ok": True, "task_id": task_id, "completed": False}

    poll = kb_tools._poll_task(task_id, "/api/server/tasks", description="ingest")
    if not poll["ok"]:
        return {"ok": False, "error": str(poll["error"]), "task_id": task_id}
    return {"ok": True, "task_id": task_id, "completed": True, "result": poll.get("result")}


@api_v1_bp.post("/kb/documents")
def kb_add_document():
    data = request.get_json(silent=True) or {}
    db = (data.get("database_name") or "").strip()
    document = data.get("document")
    filename = (data.get("filename") or "document.json").strip()
    wait = data.get("wait", True)

    if not db:
        return _err(400, "invalid_request", "'database_name' is required.")
    if not isinstance(document, dict) or not document:
        return _err(400, "invalid_request", "'document' must be a non-empty JSON object.")

    r = _kb_ingest(db, document, filename, wait=bool(wait))
    if not r["ok"]:
        return _err(502, "kb_error", "Ingestion failed.", detail=str(r["error"])[:300])
    if not r["completed"]:
        return jsonify({"task_id": r["task_id"], "state": "processing",
                        "poll": f"/api/v1/kb/tasks/{r['task_id']}"}), 202
    return jsonify({"status": "ingested", "database_name": db,
                    "filename": filename if filename.endswith(".json") else filename + ".json",
                    "task_id": r["task_id"]})


@api_v1_bp.get("/kb/tasks/<task_id>")
def kb_task(task_id):
    result = kb_tools._request("GET", f"/api/server/tasks/{task_id}", timeout=15)
    if result["status"] == 404:
        result = kb_tools._request("GET", f"/api/tasks/{task_id}", timeout=15)
    if result["error"]:
        return _err(502, "kb_error", "Task status request failed.", detail=result["error"][:300])
    data = result["data"] if isinstance(result["data"], dict) else {}
    return jsonify({
        "task_id": task_id,
        "state": data.get("state", "unknown"),
        "progress": data.get("progress", 0),
        "result": data.get("result"),
        "error": data.get("error"),
    })


# ---------------------------------------------------------------------------
# Candidates — deterministic resume → knowledge-base pipeline
# ---------------------------------------------------------------------------

@api_v1_bp.post("/candidates/save")
def candidates_save():
    data = request.get_json(silent=True) or {}
    resume_ids = data.get("resume_ids") or []
    db = (data.get("database_name") or "candidates").strip()

    if not isinstance(resume_ids, list) or not resume_ids:
        return _err(400, "invalid_request", "'resume_ids' must be a non-empty list.")
    if len(resume_ids) > 20:
        return _err(400, "invalid_request", "At most 20 resumes per request.")
    if not _has_hh_user_token():
        return _hh_auth_required_response()

    saved, failed = [], []
    for rid in resume_ids:
        rid = str(rid).strip()
        result = hh_tools._get(f"/resumes/{rid}")
        if not result["ok"]:
            failed.append({"resume_id": rid, "error": str(result["error"])[:200]})
            continue
        r = result["data"]

        exp_months = (r.get("total_experience") or {}).get("months")
        experience_list = r.get("experience") or []
        last_position = ""
        if experience_list:
            e0 = experience_list[0]
            last_position = f"{e0.get('position', '?')} — {e0.get('company', '?')}"

        document = {
            "candidate": r.get("title", "—"),
            "resume_id": rid,
            "resume_url": r.get("alternate_url", f"https://hh.ru/resume/{rid}"),
            "area": (r.get("area") or {}).get("name", ""),
            "experience_years": (exp_months // 12) if exp_months else 0,
            "key_skills": r.get("skill_set") or [],
            "specializations": [pr.get("name", "") for pr in (r.get("professional_roles") or [])],
            "salary": hh_tools._salary_str(r.get("salary")),
            "education": ((r.get("education") or {}).get("level") or {}).get("name", ""),
            "last_position": last_position,
            "source": "HeadHunter",
            "saved_at": date.today().isoformat(),
        }

        ingest = _kb_ingest(db, document, f"candidate-{rid}.json", wait=True)
        if ingest["ok"]:
            saved.append({"resume_id": rid, "resume_url": document["resume_url"],
                          "filename": f"candidate-{rid}.json"})
        else:
            failed.append({"resume_id": rid, "error": str(ingest["error"])[:200]})

    return jsonify({"database_name": db, "saved": saved, "failed": failed})


# ---------------------------------------------------------------------------
# arXiv + dashboards
# ---------------------------------------------------------------------------

@api_v1_bp.post("/arxiv/search")
def arxiv_search():
    data = request.get_json(silent=True) or {}
    user_query = (data.get("query") or "").strip()
    if not user_query:
        return _err(400, "invalid_request", "'query' is required.")
    try:
        max_results = max(1, min(int(data.get("max_results", 10)), 50))
    except (ValueError, TypeError):
        max_results = 10
    authors = (data.get("authors") or "").strip()
    # Synchronous by design: bounded at a couple of minutes (search + downloads
    # + one summarization call), well inside the gunicorn timeout.
    return jsonify(pipeline.run_search_pipeline(user_query, max_results, authors))


@api_v1_bp.get("/dashboards")
def api_dashboards():
    return jsonify({"dashboards": dashboards_store.list_all()})
