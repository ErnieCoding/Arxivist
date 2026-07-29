#!/usr/bin/env python3
"""
Agent-job runner — a dedicated process per API agent job.

Spawned by POST /api/v1/agent/jobs via Popen (start_new_session=True), so the
minutes-long agent run never occupies a gunicorn worker, and its module-level
agent session state (arxiv_tools._session) is isolated in this process — the
same guarantee the sync-worker model gives the debug chat.

Drives agent_pipeline.pipeline_events() — the exact code path /chat uses —
and persists progress into the job dir:
    status phrases → events.ndjson  (poll GET /api/v1/agent/jobs/<id>)
    final result   → status.json    (state=succeeded, result={reply, files, …})

A SIGALRM watchdog (JOB_TIMEOUT seconds, default 900) aborts runaway runs.
Cancellation is a SIGTERM from the API (jobs_store.cancel) — the process dies
and the job is already marked canceled; the finish-write below re-checks the
state under the job lock so a canceled job is never overwritten to succeeded.
"""

import logging
import os
import signal
import sys
import time

from dotenv import load_dotenv

load_dotenv()

import agent_pipeline as pipeline  # noqa: E402  (needs env loaded first)
import jobs_store  # noqa: E402

log = logging.getLogger("job_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [runner] %(levelname)s %(message)s")

JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "900"))  # seconds


class JobTimeout(Exception):
    pass


def _alarm(_sig, _frame):
    raise JobTimeout(f"job exceeded JOB_TIMEOUT={JOB_TIMEOUT}s")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: job_runner.py <job_id>", file=sys.stderr)
        return 2
    job_id = sys.argv[1]

    request = jobs_store.read_request(job_id)
    if request is None:
        log.error("job %s: request.json missing", job_id)
        return 2

    jobs_store.update_status(job_id, lambda s: s.update(
        state="running", started_at=int(time.time()), pid=os.getpid()))

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(JOB_TIMEOUT)

    result = None
    error = None
    try:
        for event in pipeline.pipeline_events(
            message=request["message"],
            history=request.get("history") or [],
            session_id=request["session_id"],
            attached_file_ids=request.get("attached_file_ids") or [],
        ):
            if event.get("type") == "status":
                jobs_store.append_event(job_id, {"phrase": event.get("phrase", "")})
            elif event.get("type") == "result":
                result = {k: v for k, v in event.items() if k != "type"}
    except JobTimeout as e:
        error = str(e)
        log.error("job %s: %s", job_id, e)
    except Exception as e:  # pipeline_events shouldn't raise, but belt-and-braces
        error = f"runner crashed: {e}"
        log.exception("job %s crashed", job_id)
    finally:
        signal.alarm(0)

    if result is None and error is None:
        error = "agent produced no result"

    def finish(s):
        # A cancel may have landed while we were finishing — never resurrect it.
        if s.get("state") in jobs_store.FINISHED_STATES:
            return
        s["finished_at"] = int(time.time())
        if error is None:
            s["state"] = "succeeded"
            s["result"] = result
        else:
            s["state"] = "failed"
            s["error"] = error

    jobs_store.update_status(job_id, finish)
    log.info("job %s finished: %s", job_id, "ok" if error is None else error)
    return 0 if error is None else 1


if __name__ == "__main__":
    sys.exit(main())
