import json
import logging
import os
import re
import sys
import urllib.request
import urllib.error

# Fix Windows console encoding — agent SDK can emit emoji/unicode that
# crashes cp1252. Force UTF-8 before any output happens.
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, jsonify, send_file, abort, redirect, url_for,
    Response, stream_with_context,
)

load_dotenv()

# agent_pipeline pulls in the SDK, MCP servers and stores; import AFTER
# load_dotenv so modules that read env at import time see .env values.
import agent_pipeline as pipeline
from arxiv_tools import DOWNLOADS_DIR
from tools import hh_auth

import dashboards_store
import uploads_store

app = Flask(__name__)

# Trust the nginx reverse proxy for correct client IP, protocol, and host headers.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


class _PrefixStripMiddleware:
    """
    Strip APP_BASE from PATH_INFO so the same Flask routes serve in two modes:

      1. Direct hit (no nginx): browser -> /arxivist/upload -> Flask sees /upload.
      2. nginx with `proxy_pass http://upstream/;` (trailing slash): nginx strips
         the prefix, Flask gets /upload directly, this middleware is a no-op.

    Without this, direct access 404s on every API endpoint because the frontend
    prepends APP_BASE to fetch URLs but Flask routes don't include the prefix.
    """

    def __init__(self, wsgi_app, prefix: str):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if self.prefix:
            path = environ.get("PATH_INFO", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                environ["PATH_INFO"] = path[len(self.prefix):] or "/"
                environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
        return self.wsgi_app(environ, start_response)


_APP_BASE_ENV = os.environ.get("APP_BASE", "").rstrip("/")
if _APP_BASE_ENV:
    app.wsgi_app = _PrefixStripMiddleware(app.wsgi_app, _APP_BASE_ENV)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# When deployed behind nginx at a sub-path (e.g. /arxivist), set SCRIPT_NAME
# in the container environment so the template generates correct fetch URLs.
SCRIPT_NAME = os.environ.get("APP_BASE", "").rstrip("/")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(PROJECT_DIR, ".claude", "skills"), exist_ok=True)
os.makedirs(os.path.join(PROJECT_DIR, "uploads"), exist_ok=True)
os.makedirs(os.path.join(PROJECT_DIR, "dashboards"), exist_ok=True)

DASHBOARD_UUID_RE = re.compile(r"^[a-f0-9]{32}$")


@app.route("/")
def index():
    return render_template("index.html", base=SCRIPT_NAME)


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
    return jsonify(pipeline.run_search_pipeline(user_query, max_results, authors))


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.route("/chat", methods=["POST"])
def chat():
    """
    Streams Server-Sent Events:
      {"type":"status","phrase":"…"}  — progress phrases shown WHILE processing
      {"type":"result", ...}          — final answer + files (rendered as the reply)
    The final answer contains no internal mechanics (enforced by the system prompt).
    """
    data = request.get_json()
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    session_id = (data.get("session_id") or "").strip()
    attached_file_ids = data.get("attached_file_ids") or []

    if not message:
        return jsonify({"error": "Please enter a message."}), 400
    if not session_id:
        return jsonify({"error": "Missing session_id."}), 400

    log.info(
        "Chat message received: %r | history=%d | session=%s | attached=%d",
        message, len(history), session_id, len(attached_file_ids),
    )

    def generate():
        for event in pipeline.pipeline_events(message, history, session_id, attached_file_ids):
            yield _sse(event)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx response buffering for SSE
        },
    )


@app.route("/files")
def list_all_files():
    files = []
    if os.path.exists(DOWNLOADS_DIR):
        for f in sorted(os.listdir(DOWNLOADS_DIR)):
            if f.lower().endswith(".pdf"):
                path = os.path.join(DOWNLOADS_DIR, f)
                size_kb = os.path.getsize(path) / 1024
                files.append({"name": f, "size_kb": round(size_kb, 1)})
    return jsonify({"files": files})


@app.route("/upload", methods=["POST"])
def upload():
    """Accept one or more file attachments. Returns per-file metadata."""
    if "file" not in request.files:
        return jsonify({"error": "No file part in request."}), 400

    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "No files attached."}), 400

    saved = []
    errors = []
    for fs in files:
        if not fs or not fs.filename:
            errors.append({"name": "?", "detail": "empty filename"})
            continue
        try:
            meta = uploads_store.save_upload(fs)
        except uploads_store.UploadError as e:
            errors.append({"name": fs.filename, "detail": str(e)})
        except Exception as e:
            log.exception("Upload failed for %s", fs.filename)
            errors.append({"name": fs.filename, "detail": f"Upload failed: {e}"})
        else:
            saved.append(meta)

    log.info("Upload received: %d saved, %d errors", len(saved), len(errors))
    return jsonify({"files": saved, "errors": errors})


@app.route("/d/<dashboard_uuid>")
def serve_dashboard(dashboard_uuid):
    """Serve a previously generated dashboard's HTML."""
    if not DASHBOARD_UUID_RE.match(dashboard_uuid):
        abort(404)
    path = dashboards_store.path_for(dashboard_uuid)
    if not path:
        abort(404)
    resp = send_file(path, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/dashboards")
def list_dashboards():
    """Return all registered dashboards for the sidebar (newest first)."""
    return jsonify({"dashboards": dashboards_store.list_all()})


_BRIDGE_BASE = f"http://{os.environ.get('BRIDGE_FAKE_HOST', '127.0.0.1')}:{os.environ.get('BRIDGE_FAKE_PORT', '9999')}"


@app.route("/api/proxy-mode", methods=["GET", "POST"])
def proxy_mode():
    """
    GET  → returns {"mode": "direct"|"proxy", "proxy_available": bool, "direct_available": bool}
    POST {"mode": "direct"|"proxy"} → switches the bridge mode, returns same shape
    """
    bridge_url = f"{_BRIDGE_BASE}/mode"
    try:
        if request.method == "POST":
            data = request.get_json(force=True) or {}
            payload = json.dumps({"mode": data.get("mode", "")}).encode("utf-8")
            req = urllib.request.Request(
                bridge_url, data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
        else:
            req = urllib.request.Request(bridge_url, method="GET")

        with urllib.request.urlopen(req, timeout=5) as resp:
            return jsonify(json.loads(resp.read()))

    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return jsonify({"error": f"Bridge returned HTTP {e.code}: {err}"}), 502
    except Exception as e:
        return jsonify({"error": f"Bridge unreachable: {e}"}), 502


# ---------------------------------------------------------------------------
# HeadHunter OAuth2 — one-time browser flow to obtain a user access token.
#   /hh/authorize → redirects to hh.ru consent screen
#   /hh/callback  → hh.ru redirects back here with ?code=…, we exchange + store
#   /hh/status    → JSON status of the current token
# The token is persisted to config/hh_token.json and read by tools/hh_tools.py.
# ---------------------------------------------------------------------------

def _hh_redirect_uri() -> str:
    """
    The redirect URI to hand HH. An explicit HH_REDIRECT_URI env wins (pin it
    if your registered value differs); otherwise derive it from the current
    request so the same code works on localhost and behind the nginx sub-path.
    Must match a Redirect URI registered in the HH app.
    """
    return hh_auth.HH_REDIRECT_URI or url_for("hh_callback", _external=True)


def _hh_page(title: str, body_html: str, status: int = 200, extra_script: str = ""):
    """Small dark-themed page shell for the OAuth screens, matching the app UI."""
    home = (SCRIPT_NAME or "") + "/"
    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ background:#0f1117; color:#e0e0e0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:#1a1d27; border:1px solid #2a2d3a; border-radius:14px; padding:2.2rem 2.6rem;
          max-width:520px; text-align:center; box-shadow:0 8px 32px rgba(0,0,0,.35); }}
  h2 {{ margin:0 0 .8rem; color:#fff; font-size:1.25rem; }}
  p  {{ margin:.4rem 0; line-height:1.55; color:#b9c0d4; font-size:.92rem; }}
  a  {{ color:#93b0ff; }}
  .ok {{ color:#4ade80; font-size:2.2rem; display:block; margin-bottom:.6rem; }}
  .err {{ color:#f87171; font-size:2.2rem; display:block; margin-bottom:.6rem; }}
  .hint {{ color:#6b7288; font-size:.8rem; margin-top:1.1rem; }}
</style></head>
<body><div class="card">{body_html}
<p class="hint"><a href="{home}">← Вернуться в Arxivist</a></p>
</div>{extra_script}</body></html>"""
    return html, status


@app.route("/hh/authorize")
def hh_authorize():
    if not hh_auth.HH_CLIENT_ID:
        return _hh_page(
            "HeadHunter — не настроено",
            "<span class='err'>✕</span><h2>Интеграция не настроена</h2>"
            "<p>HH_CLIENT_ID / HH_CLIENT_SECRET не заданы в .env. Задайте их и перезапустите приложение.</p>",
            status=400,
        )
    state = hh_auth.new_state()
    return redirect(hh_auth.build_authorize_url(redirect_uri=_hh_redirect_uri(), state=state))


@app.route("/hh/callback")
def hh_callback():
    error = request.args.get("error")
    if error:
        desc = request.args.get("error_description", "")
        return _hh_page(
            "HeadHunter — ошибка",
            f"<span class='err'>✕</span><h2>Авторизация не завершилась</h2><p>{error}: {desc}</p>"
            "<p>Вернитесь в чат и попробуйте войти ещё раз по ссылке от ассистента.</p>",
            status=400,
        )

    code = request.args.get("code")
    if not code:
        return _hh_page(
            "HeadHunter — ошибка",
            "<span class='err'>✕</span><h2>Не получен код авторизации</h2>"
            "<p>Похоже, страница открыта напрямую. Начните вход по ссылке от ассистента.</p>",
            status=400,
        )

    # CSRF check: the state must match the one minted at /hh/authorize.
    if not hh_auth.consume_state(request.args.get("state", "")):
        return _hh_page(
            "HeadHunter — ошибка",
            "<span class='err'>✕</span><h2>Сессия авторизации устарела</h2>"
            "<p>Ссылка входа была открыта повторно или истекла. Вернитесь в чат и начните вход заново.</p>",
            status=400,
        )

    result = hh_auth.exchange_code(code, redirect_uri=_hh_redirect_uri())
    if not result["ok"]:
        log.error("HH OAuth token exchange failed: %s", result["error"])
        return _hh_page(
            "HeadHunter — ошибка",
            "<span class='err'>✕</span><h2>Не удалось обменять код на токен</h2>"
            "<p>Попробуйте войти ещё раз. Если ошибка повторяется — проверьте, что Redirect URI "
            "в настройках приложения на dev.hh.ru совпадает с адресом этой страницы.</p>",
            status=502,
        )

    log.info("HH OAuth: user token obtained and stored")
    # Auto-return: notify the chat tab (same-origin BroadcastChannel) and close
    # this tab. The chat page ALSO polls /hh/status as a fallback, so the flow
    # continues even if this tab can't signal (different origin) or won't close.
    script = """<script>
  try { new BroadcastChannel('arxivist-hh-auth').postMessage({ type: 'hh-connected' }); } catch (e) {}
  setTimeout(function () { window.close(); }, 1200);
</script>"""
    return _hh_page(
        "HeadHunter подключён",
        "<span class='ok'>✓</span><h2>HeadHunter подключён</h2>"
        "<p>Поиск кандидатов и резюме теперь доступен ассистенту.</p>"
        "<p>Эта вкладка закроется сама — диалог продолжится в чате автоматически.</p>",
        extra_script=script,
    )


@app.route("/hh/status")
def hh_status():
    return jsonify(hh_auth.token_status())


# ---------------------------------------------------------------------------
# Versioned, API-key-gated backend module for external consumers.
# All /api/v1/* routes live in api_v1.py (blueprint); auth is enforced by its
# before_request hook against the hashed keystore in config/api_keys.json.
# ---------------------------------------------------------------------------
from api_v1 import api_v1_bp
app.register_blueprint(api_v1_bp)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
