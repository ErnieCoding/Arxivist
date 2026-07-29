"""
API-key store for the /api/v1 backend module.

Design (mirrors the repo's two proven runtime-state patterns):
  - validation reads the file on every request  → like hh_auth.load_access_token()
  - writes go through an fcntl.flock exclusive lock → like dashboards_store

Key format:  axv_<key_id>_<secret>
  - key_id: 8 hex chars — safe to log/audit, gives O(1) record lookup
  - secret: secrets.token_urlsafe(32) (~256 bits of entropy)

Only sha256(full_key) is stored at rest (config/api_keys.json, gitignored,
bind-mounted volume) — a leaked backup of config/ does not disclose usable
keys. Verification uses hmac.compare_digest (constant-time).

Revocation/rotation happens WITHOUT restarting the app: the store is re-read
per request, so `scripts/apikeys.py revoke <id>` takes effect immediately on
all gunicorn workers. This matters because a restart would kill in-flight
minutes-long agent jobs.
"""

import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time

log = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_THIS_DIR)
KEYS_FILE = os.path.join(_PROJECT_DIR, "config", "api_keys.json")
LOCK_FILE = os.path.join(_PROJECT_DIR, "config", ".api_keys.lock")

KEY_RE = re.compile(r"^axv_([0-9a-f]{8})_([A-Za-z0-9_\-]{20,})$")

# last_used_at is refreshed at most once per this interval, so routine
# request validation stays read-only (no lock contention on the hot path).
_LAST_USED_WRITE_INTERVAL = 300  # seconds


def _locked(fn):
    """Run fn() under an exclusive flock on LOCK_FILE."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _read() -> dict:
    try:
        with open(KEYS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"keys": []}
    data.setdefault("keys", [])
    return data


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, KEYS_FILE)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except OSError:
        pass


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def create_key(label: str) -> dict:
    """
    Mint a new API key. Returns {"key": <full key — shown ONCE>, "key_id", "label"}.
    Only the hash is persisted.
    """
    key_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full_key = f"axv_{key_id}_{secret}"

    record = {
        "key_id": key_id,
        "label": label or "unnamed",
        "sha256": _sha256(full_key),
        "created_at": int(time.time()),
        "revoked": False,
        "last_used_at": None,
    }

    def txn():
        data = _read()
        data["keys"].append(record)
        _write(data)

    _locked(txn)
    log.info("API key created: id=%s label=%r", key_id, label)
    return {"key": full_key, "key_id": key_id, "label": record["label"]}


def verify_key(presented: str) -> dict | None:
    """
    Validate a presented key. Returns the key record (metadata, no hash) on
    success, None otherwise. Constant-time hash comparison; the key_id prefix
    is only used for record lookup, never as proof.
    """
    m = KEY_RE.match(presented or "")
    if not m:
        return None
    key_id = m.group(1)

    data = _read()
    for rec in data["keys"]:
        if rec.get("key_id") == key_id and not rec.get("revoked"):
            if hmac.compare_digest(rec.get("sha256", ""), _sha256(presented)):
                _touch_last_used(key_id, rec.get("last_used_at"))
                return {k: v for k, v in rec.items() if k != "sha256"}
            return None  # id matched but secret didn't — do not keep scanning
    return None


def _touch_last_used(key_id: str, prev: float | None) -> None:
    """Update last_used_at, throttled so the hot path stays read-only."""
    now = time.time()
    if prev is not None and now - float(prev) < _LAST_USED_WRITE_INTERVAL:
        return

    def txn():
        data = _read()
        for rec in data["keys"]:
            if rec.get("key_id") == key_id:
                rec["last_used_at"] = int(now)
        _write(data)

    try:
        _locked(txn)
    except OSError as e:
        log.warning("Could not update last_used_at for %s: %s", key_id, e)


def list_keys() -> list[dict]:
    """All key records, hashes redacted."""
    return [{k: v for k, v in rec.items() if k != "sha256"} for rec in _read()["keys"]]


def revoke_key(key_id: str) -> bool:
    """Mark a key revoked. Takes effect on the next request (no restart)."""
    found = []

    def txn():
        data = _read()
        for rec in data["keys"]:
            if rec.get("key_id") == key_id and not rec.get("revoked"):
                rec["revoked"] = True
                rec["revoked_at"] = int(time.time())
                found.append(True)
        _write(data)

    _locked(txn)
    if found:
        log.info("API key revoked: id=%s", key_id)
    return bool(found)
