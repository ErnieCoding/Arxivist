"""
HeadHunter OAuth2 token management.

Shared between app.py (Flask callback routes that obtain/store tokens) and
hh_tools.py (which reads the token for API calls). Tokens are persisted to
config/hh_token.json on the bind-mounted config volume so they survive restarts.

HH OAuth grant types:
  - authorization_code : user token (needed for resumes, negotiations). Requires
                         the browser redirect flow → this module's exchange_code().
  - client_credentials : application token (higher rate limits, app-level data).
                         No user interaction → get_app_token().
  - refresh_token      : refresh an expired user token.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_THIS_DIR)
TOKEN_FILE = os.path.join(_PROJECT_DIR, "config", "hh_token.json")

HH_TOKEN_URL = "https://hh.ru/oauth/token"
HH_AUTHORIZE_URL = "https://hh.ru/oauth/authorize"

HH_CLIENT_ID = os.environ.get("HH_CLIENT_ID", "").strip()
HH_CLIENT_SECRET = os.environ.get("HH_CLIENT_SECRET", "").strip()
# Empty by default so the Flask layer can derive the redirect URI from the
# actual request host (works for both localhost and the deployed domain).
# Set HH_REDIRECT_URI in .env only if you must pin it to a specific value.
HH_REDIRECT_URI = os.environ.get("HH_REDIRECT_URI", "").strip()


def _post_form(fields: dict, timeout: int = 30) -> dict:
    """POST application/x-www-form-urlencoded to the HH token endpoint."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        HH_TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Arxivist/1.0 (info@cyberskill.net)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _save(token: dict) -> None:
    """Persist a token dict, stamping expires_at from expires_in."""
    if "expires_in" in token:
        try:
            token["expires_at"] = time.time() + int(token["expires_in"])
        except (ValueError, TypeError):
            pass
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump(token, f, indent=2)
        log.info("HH token saved to %s", TOKEN_FILE)
    except OSError as e:
        log.error("Could not persist HH token: %s", e)


def _read() -> dict:
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def build_authorize_url(redirect_uri: str = "", state: str = "") -> str:
    """Build the HH authorization URL to send the user's browser to."""
    params = {
        "response_type": "code",
        "client_id": HH_CLIENT_ID,
        "redirect_uri": redirect_uri or HH_REDIRECT_URI,
    }
    if state:
        params["state"] = state
    return f"{HH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, redirect_uri: str = "") -> dict:
    """Exchange an authorization code for a user token. Persists on success.

    redirect_uri MUST match the one used in build_authorize_url() for this code.
    """
    if not (HH_CLIENT_ID and HH_CLIENT_SECRET):
        return {"ok": False, "error": "HH_CLIENT_ID / HH_CLIENT_SECRET not configured"}
    r = _post_form({
        "grant_type": "authorization_code",
        "client_id": HH_CLIENT_ID,
        "client_secret": HH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": redirect_uri or HH_REDIRECT_URI,
    })
    if r["ok"]:
        token = dict(r["data"])
        token["grant"] = "authorization_code"
        _save(token)
        return {"ok": True, "data": token}
    return r


def get_app_token() -> dict:
    """Obtain a client_credentials application token. Persists on success."""
    if not (HH_CLIENT_ID and HH_CLIENT_SECRET):
        return {"ok": False, "error": "HH_CLIENT_ID / HH_CLIENT_SECRET not configured"}
    r = _post_form({
        "grant_type": "client_credentials",
        "client_id": HH_CLIENT_ID,
        "client_secret": HH_CLIENT_SECRET,
    })
    if r["ok"]:
        token = dict(r["data"])
        token["grant"] = "client_credentials"
        _save(token)
        return {"ok": True, "data": token}
    return r


def _refresh(refresh_token: str) -> dict:
    r = _post_form({"grant_type": "refresh_token", "refresh_token": refresh_token})
    if r["ok"]:
        token = dict(r["data"])
        token["grant"] = "refresh_token"
        _save(token)
        return {"ok": True, "data": token}
    return r


def load_access_token() -> str:
    """
    Return a usable access token for API calls, or "" if none.
    Order: valid saved token → refreshed token → HH_ACCESS_TOKEN env fallback.
    Called on every HH API request so a freshly-saved token is picked up
    without a restart.
    """
    tok = _read()
    access = tok.get("access_token", "")
    expires_at = tok.get("expires_at")
    refresh_token = tok.get("refresh_token")

    if access:
        # Still valid (60s safety margin)?
        if not expires_at or time.time() < float(expires_at) - 60:
            return access
        # Expired — try refresh.
        if refresh_token:
            log.info("HH token expired; refreshing…")
            r = _refresh(refresh_token)
            if r["ok"]:
                return r["data"].get("access_token", "")
            log.warning("HH token refresh failed: %s", r.get("error"))
        # If it's an app token with no refresh, mint a new one.
        if tok.get("grant") == "client_credentials":
            r = get_app_token()
            if r["ok"]:
                return r["data"].get("access_token", "")
        return access  # last resort: return possibly-expired token

    # No stored token. Prefer an explicit env token; otherwise, if app
    # credentials are configured, transparently mint a client_credentials
    # (application) token — this is what HH vacancy/employer search now requires.
    env_token = os.environ.get("HH_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    if HH_CLIENT_ID and HH_CLIENT_SECRET:
        log.info("No HH token stored; minting client_credentials app token")
        r = get_app_token()
        if r["ok"]:
            return r["data"].get("access_token", "")
        log.warning("HH app-token request failed: %s", r.get("error"))
    return ""


def token_status() -> dict:
    """Human-readable status of the current token, for the /hh/status route."""
    tok = _read()
    if not tok and not os.environ.get("HH_ACCESS_TOKEN"):
        return {"configured": False, "message": "No HH token stored. Visit /hh/authorize to obtain one."}
    expires_at = tok.get("expires_at")
    remaining = int(expires_at - time.time()) if expires_at else None
    return {
        "configured": True,
        "grant": tok.get("grant", "env" if os.environ.get("HH_ACCESS_TOKEN") else "unknown"),
        "has_refresh_token": bool(tok.get("refresh_token")),
        "expires_in_seconds": remaining,
        "expired": (remaining is not None and remaining <= 0),
        "client_id_configured": bool(HH_CLIENT_ID),
        "redirect_uri": HH_REDIRECT_URI,
    }
