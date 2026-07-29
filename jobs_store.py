"""
File-based store for asynchronous agent jobs (the API's 202+poll pattern).

Why files and a subprocess instead of Celery/Redis: the repo's constraint set
(module-level agent session state → process isolation required, no external
infrastructure, everything persisted under bind-mounted dirs with flock) is
already served by this exact pattern elsewhere (bridge sibling process,
dashboards registry). A queue broker would add two services for zero gain at
this scale; the HTTP contract (202 + GET status) would not change if one is
introduced later.

Layout: jobs/<job_id>/
    request.json   — the submitted payload (immutable)
    status.json    — {job_id, state, created_at, started_at, finished_at,
                      pid, result, error}   (atomic replace on write)
    events.ndjson  — one JSON status-phrase event per line (runner appends)
    runner.log     — runner process stdout/stderr

States: queued → running → succeeded | failed | canceled.

Concurrency: status.json read-modify-writes go through a per-job flock
(.lock), because two processes touch it (the runner and a gunicorn worker
handling DELETE /cancel). Event appends are single-writer (runner only).
"""

import contextlib
import fcntl
import json
import logging
import os
import shutil
import signal
import time
import uuid

log = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(PROJECT_DIR, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

FINISHED_STATES = ("succeeded", "failed", "canceled")
JOB_TTL_DAYS = float(os.environ.get("JOB_TTL_DAYS", "7"))


def _job_dir(job_id: str) -> str:
    return os.path.join(JOBS_DIR, job_id)


def _status_path(job_id: str) -> str:
    return os.path.join(_job_dir(job_id), "status.json")


@contextlib.contextmanager
def _locked(job_id: str):
    with open(os.path.join(_job_dir(job_id), ".lock"), "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _write_status(job_id: str, status: dict) -> None:
    path = _status_path(job_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_status_raw(job_id: str) -> dict | None:
    try:
        with open(_status_path(job_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def create_job(payload: dict) -> str:
    """Persist a new queued job; returns its id. Also sweeps expired jobs."""
    sweep()
    job_id = uuid.uuid4().hex
    d = _job_dir(job_id)
    os.makedirs(d, exist_ok=True)

    with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    _write_status(job_id, {
        "job_id": job_id,
        "state": "queued",
        "created_at": int(time.time()),
        "started_at": None,
        "finished_at": None,
        "pid": None,
        "result": None,
        "error": None,
    })
    return job_id


def read_request(job_id: str) -> dict | None:
    try:
        with open(os.path.join(_job_dir(job_id), "request.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def update_status(job_id: str, mutate) -> dict | None:
    """
    Read-modify-write status.json under the per-job lock. `mutate(status)`
    edits the dict in place; return the new status (or None if job missing).
    """
    if not os.path.isdir(_job_dir(job_id)):
        return None
    with _locked(job_id):
        status = _read_status_raw(job_id)
        if status is None:
            return None
        mutate(status)
        _write_status(job_id, status)
        return status


def read_status(job_id: str) -> dict | None:
    """
    Current job status. Self-heals a stale 'running' entry whose runner
    process is gone (container restart, OOM kill): marks it failed so
    clients never poll a zombie forever.
    """
    status = _read_status_raw(job_id)
    if status is None:
        return None
    if status.get("state") == "running" and not _pid_alive(status.get("pid")):
        def fix(s):
            if s.get("state") == "running" and not _pid_alive(s.get("pid")):
                s["state"] = "failed"
                s["finished_at"] = int(time.time())
                s["error"] = "runner process died unexpectedly (app restart?)"
        status = update_status(job_id, fix) or status
    return status


def append_event(job_id: str, event: dict) -> None:
    """Append one progress event (runner is the only writer)."""
    event = {"ts": int(time.time()), **event}
    with open(os.path.join(_job_dir(job_id), "events.ndjson"), "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(job_id: str, limit: int = 50) -> list[dict]:
    try:
        with open(os.path.join(_job_dir(job_id), "events.ndjson"), encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def active_count() -> int:
    """
    Jobs occupying capacity: running with a live runner, plus recently queued
    ones whose runner hasn't started yet (so a submit burst can't oversubscribe
    between create_job() and the runner's first status write).
    """
    count = 0
    now = time.time()
    try:
        ids = os.listdir(JOBS_DIR)
    except OSError:
        return 0
    for job_id in ids:
        status = _read_status_raw(job_id)
        if not status:
            continue
        state = status.get("state")
        if state == "running" and _pid_alive(status.get("pid")):
            count += 1
        elif state == "queued" and now - float(status.get("created_at") or 0) < 300:
            count += 1
    return count


def cancel(job_id: str) -> dict | None:
    """SIGTERM the runner (if alive) and mark the job canceled."""
    status = _read_status_raw(job_id)
    if status is None:
        return None
    if status.get("state") in FINISHED_STATES:
        return status

    pid = status.get("pid")
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError as e:
            log.warning("Could not signal job %s runner (pid %s): %s", job_id, pid, e)

    def mark(s):
        if s.get("state") not in FINISHED_STATES:
            s["state"] = "canceled"
            s["finished_at"] = int(time.time())
    return update_status(job_id, mark)


def sweep(ttl_days: float = JOB_TTL_DAYS) -> None:
    """Best-effort removal of finished jobs older than the TTL."""
    cutoff = time.time() - ttl_days * 86400
    try:
        ids = os.listdir(JOBS_DIR)
    except OSError:
        return
    for job_id in ids:
        status = _read_status_raw(job_id)
        if status is None:
            continue
        finished = status.get("finished_at")
        if status.get("state") in FINISHED_STATES and finished and finished < cutoff:
            shutil.rmtree(_job_dir(job_id), ignore_errors=True)
