"""
Dashboard registry and storage.

Each registered dashboard lives at dashboards/<uuid>/index.html. The registry
(dashboards/registry.json) maps session_id -> created/active UUIDs and stores
per-dashboard metadata for the sidebar listing.

All read-modify-write cycles on registry.json are protected by an fcntl.flock
exclusive lock on dashboards/.registry.lock — required because gunicorn runs
two worker processes that may race on dashboard creation.

POSIX-only (fcntl). Documented as a Windows-dev limitation; the production
container is Linux.
"""

import contextlib
import fcntl
import json
import logging
import os
import re
import time
import uuid

log = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARDS_DIR = os.path.join(PROJECT_DIR, "dashboards")
os.makedirs(DASHBOARDS_DIR, exist_ok=True)

REGISTRY_PATH = os.path.join(DASHBOARDS_DIR, "registry.json")
LOCK_PATH = os.path.join(DASHBOARDS_DIR, ".registry.lock")

APP_BASE = os.environ.get("APP_BASE", "").rstrip("/")

UUID_RE = re.compile(r"^[a-f0-9]{32}$")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@contextlib.contextmanager
def _locked():
    """Acquire an exclusive flock on the registry lock file."""
    with open(LOCK_PATH, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _read_registry() -> dict:
    if not os.path.exists(REGISTRY_PATH):
        return {"sessions": {}, "dashboards": {}}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("Registry read failed (%s); starting from empty.", e)
        return {"sessions": {}, "dashboards": {}}
    data.setdefault("sessions", {})
    data.setdefault("dashboards", {})
    return data


def _write_registry(data: dict) -> None:
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY_PATH)


def _extract_title(html: str) -> str:
    m = _TITLE_RE.search(html)
    if m:
        title = m.group(1).strip()
        if title:
            return title[:200]
    return ""


def _url_for(dashboard_uuid: str) -> str:
    return f"{APP_BASE}/d/{dashboard_uuid}"


def register(session_id: str, html_str: str, title: str = "") -> dict:
    """
    Persist a new dashboard atomically.

    Mints a UUID, writes dashboards/<uuid>/index.html, registers it under
    sessions[session_id] and dashboards[uuid], and sets it active for the
    session. Returns {uuid, url, title}.
    """
    if not session_id:
        raise ValueError("session_id is required")
    if not html_str or not html_str.strip():
        raise ValueError("html is required")

    dashboard_uuid = uuid.uuid4().hex
    if not title:
        title = _extract_title(html_str) or dashboard_uuid[:8]

    dest_dir = os.path.join(DASHBOARDS_DIR, dashboard_uuid)
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_str)

    with _locked():
        data = _read_registry()
        data["sessions"].setdefault(session_id, {"active": None, "created": []})
        data["sessions"][session_id]["active"] = dashboard_uuid
        data["sessions"][session_id]["created"].append(dashboard_uuid)
        data["dashboards"][dashboard_uuid] = {
            "session_id": session_id,
            "title": title,
            "created_at": int(time.time()),
        }
        _write_registry(data)

    log.info("Registered dashboard %s for session %s (title=%r)",
             dashboard_uuid, session_id, title)
    return {"uuid": dashboard_uuid, "url": _url_for(dashboard_uuid), "title": title}


def get_active(session_id: str) -> str | None:
    """Return the active dashboard UUID for a session, or None."""
    if not session_id:
        return None
    with _locked():
        data = _read_registry()
        sess = data["sessions"].get(session_id)
        if not sess:
            return None
        return sess.get("active")


def set_active(session_id: str, dashboard_uuid: str) -> None:
    """Explicitly mark a dashboard as the active one for a session."""
    with _locked():
        data = _read_registry()
        if dashboard_uuid not in data["dashboards"]:
            raise KeyError(f"Unknown dashboard: {dashboard_uuid}")
        data["sessions"].setdefault(session_id, {"active": None, "created": []})
        data["sessions"][session_id]["active"] = dashboard_uuid
        _write_registry(data)


def list_all() -> list[dict]:
    """
    Return all registered dashboards as a list of
    {uuid, title, created_at, session_id}, sorted newest first.
    """
    with _locked():
        data = _read_registry()
    rows = []
    for u, meta in data["dashboards"].items():
        rows.append({
            "uuid": u,
            "title": meta.get("title", u[:8]),
            "created_at": meta.get("created_at", 0),
            "session_id": meta.get("session_id", ""),
        })
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


def path_for(dashboard_uuid: str) -> str | None:
    """Return the on-disk path for a dashboard's HTML, or None if missing."""
    if not UUID_RE.match(dashboard_uuid):
        return None
    path = os.path.join(DASHBOARDS_DIR, dashboard_uuid, "index.html")
    return path if os.path.exists(path) else None
