"""
SSE-pull bridge for proxies that respond with 202 + task_id and expose a
GET endpoint that streams the actual Anthropic response as Server-Sent
Events.

Flow:

    bundled CLI / Anthropic SDK
        |
        |  POST http://127.0.0.1:9999/v1/messages
        |  (Authorization: Bearer <ignored>)
        v
    ┌─────────────── bridge ──────────────────┐
    | 1. Forward POST to upstream proxy.      |
    | 2. Receive 202 + task_id.               |
    | 3. Open GET to the upstream stream URL  |
    |    for that task_id.                    |
    | 4. Pipe SSE chunks to the original      |
    |    client (when stream:true), OR        |
    |    collect events into a Message dict   |
    |    and return JSON (when stream:false). |
    └─────────────────────────────────────────┘
        |                                 |
        |  POST upstream/v1/messages      |  GET upstream/stream/<task_id>
        |  (Authorization: Bearer ...)    |  (Authorization: Bearer ...)
        v                                 v
    Real upstream proxy

Required env vars:
    PROXY_UPSTREAM_URL          e.g. https://ai.rndl.ru/proxy/anthropic
                                used for the POST step
    PROXY_AUTH_TOKEN            Bearer token issued by the proxy operator
    PROXY_STREAM_URL_TEMPLATE   URL template with {task_id} placeholder
                                e.g. https://ai.rndl.ru/proxy/stream/{task_id}

Optional env vars:
    BRIDGE_TIMEOUT              total seconds the bridge waits for the
                                stream to complete (default 600)
    BRIDGE_STREAM_OPEN_RETRIES  retries when the stream GET returns 404
                                because the task isn't queued yet
                                (default 5; small backoff between retries)
    BRIDGE_FAKE_HOST            host to bind fake API on (default 127.0.0.1)
    BRIDGE_FAKE_PORT            port to bind fake API on (default 9999)
    BRIDGE_LOG_LEVEL            DEBUG|INFO|WARNING|ERROR (default INFO)
    BRIDGE_STRIP_FIELDS         comma-separated body fields to drop before
                                forwarding (default: context_management)
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional

from aiohttp import web, ClientSession, ClientTimeout

log = logging.getLogger("bridge")


def _env_required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Required env var {name} is not set")
    return v.strip()


# Required config
PROXY_UPSTREAM_URL = _env_required("PROXY_UPSTREAM_URL").rstrip("/")
PROXY_AUTH_TOKEN = _env_required("PROXY_AUTH_TOKEN")
PROXY_STREAM_URL_TEMPLATE = _env_required("PROXY_STREAM_URL_TEMPLATE")
if "{task_id}" not in PROXY_STREAM_URL_TEMPLATE:
    raise RuntimeError(
        "PROXY_STREAM_URL_TEMPLATE must contain the literal placeholder "
        "{task_id} — e.g. https://ai.rndl.ru/proxy/stream/{task_id}"
    )

# Optional config
BRIDGE_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "600"))
BRIDGE_STREAM_OPEN_RETRIES = int(os.environ.get("BRIDGE_STREAM_OPEN_RETRIES", "5"))
BRIDGE_FAKE_HOST = os.environ.get("BRIDGE_FAKE_HOST", "127.0.0.1")
BRIDGE_FAKE_PORT = int(os.environ.get("BRIDGE_FAKE_PORT", "9999"))
BRIDGE_LOG_LEVEL = os.environ.get("BRIDGE_LOG_LEVEL", "INFO").upper()

BRIDGE_STRIP_FIELDS = {
    f.strip()
    for f in os.environ.get("BRIDGE_STRIP_FIELDS", "context_management").split(",")
    if f.strip()
}


# ---------------------------------------------------------------------------
# Helpers: SSE encoding / decoding
# ---------------------------------------------------------------------------

def _unwrap_response(body):
    """If the proxy wraps a sync 200 JSON response, find the Anthropic dict."""
    if not isinstance(body, dict):
        return body
    if "content" in body and ("role" in body or "model" in body):
        return body
    for key in ("result", "data", "response", "message", "payload"):
        inner = body.get(key)
        if isinstance(inner, dict) and "content" in inner and ("role" in inner or "model" in inner):
            return inner
    return body


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _synthesize_sse(message: dict) -> bytes:
    """
    Synthesize an SSE event stream from a complete Anthropic Messages response.
    Only used when the proxy returns a sync 200 + JSON body but the client
    requested stream:true. In the normal flow, SSE comes from upstream and is
    piped through unchanged.
    """
    msg_id = message.get("id") or f"msg_{uuid.uuid4().hex}"
    model = message.get("model", "claude-sonnet-4")
    role = message.get("role", "assistant")
    usage = message.get("usage", {}) or {}

    out: list[bytes] = []
    out.append(_sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": role, "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage,
        },
    }))

    for idx, block in enumerate(message.get("content", [])):
        bt = block.get("type", "text")
        if bt == "text":
            out.append(_sse_event("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""},
            }))
            out.append(_sse_event("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "text_delta", "text": block.get("text", "")},
            }))
        elif bt == "tool_use":
            out.append(_sse_event("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {
                    "type": "tool_use", "id": block.get("id"),
                    "name": block.get("name"), "input": {},
                },
            }))
            out.append(_sse_event("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            }))
        else:
            out.append(_sse_event("content_block_start", {
                "type": "content_block_start", "index": idx, "content_block": block,
            }))
        out.append(_sse_event("content_block_stop", {
            "type": "content_block_stop", "index": idx,
        }))

    out.append(_sse_event("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": message.get("stop_reason", "end_turn"),
            "stop_sequence": message.get("stop_sequence"),
        },
        "usage": usage,
    }))
    out.append(_sse_event("message_stop", {"type": "message_stop"}))
    return b"".join(out)


def _parse_sse_event(event_text: str) -> Optional[dict]:
    """
    Parse one SSE event block (lines separated by \\n, blocks by \\n\\n).
    Returns the JSON-decoded data field, or None if the block is empty/comment.
    """
    data_lines = []
    for raw in event_text.split("\n"):
        line = raw.rstrip("\r")
        if not line or line.startswith(":"):  # comment / heartbeat
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # event:/id:/retry: lines are ignored — we always look at data

    if not data_lines:
        return None
    data_str = "\n".join(data_lines)
    if data_str.strip() == "[DONE]":
        return {"type": "_done_sentinel"}
    try:
        return json.loads(data_str)
    except json.JSONDecodeError:
        log.warning("SSE data wasn't JSON: %r", data_str[:200])
        return None


def _detect_sse_format(events: list[str]) -> str:
    """
    Decide whether an SSE stream is canonical Anthropic events or the
    custom envelope the upstream proxy emits.
    Returns "anthropic", "proxy", or "unknown".
    """
    for ev_text in events[:6]:
        ev = _parse_sse_event(ev_text)
        if not ev:
            continue
        if ev.get("type") in (
            "message_start", "message_delta", "message_stop", "ping",
            "content_block_start", "content_block_delta", "content_block_stop",
        ):
            return "anthropic"
        if "status" in ev and ("delta" in ev or "response" in ev):
            return "proxy"
    return "unknown"


def _proxy_events_to_message(events: list[str]) -> Optional[dict]:
    """
    Translate the upstream proxy's wrapper format into an Anthropic Message
    dict. The proxy emits:
        data: {"status":"stream", "task_id":"...", "delta":"<chunk>"}
        ... (one per chunk)
        data: {"status":"done", "task_id":"...", "response":"<full>", "usage":{...}}
    Only the text content survives this format — the proxy strips
    tool_use blocks, content-block structure, role, stop_reason, etc.
    For text-only assistant turns this is enough to keep the agent loop
    moving. Anything that requires tool_use will fail at the agent level
    even when this translator runs cleanly.
    """
    streamed = ""
    final_text = None
    usage = None
    task_id = None
    looks_like_json = False
    proxy_error = None

    for ev_text in events:
        ev = _parse_sse_event(ev_text)
        if not ev:
            continue
        status = ev.get("status")
        task_id = ev.get("task_id") or task_id

        if status == "stream":
            delta = ev.get("delta", "")
            if isinstance(delta, str):
                streamed += delta
                if streamed and streamed[0] in "{[":
                    looks_like_json = True

        elif status == "done":
            resp = ev.get("response")
            if isinstance(resp, dict):
                # If the proxy ever fixes serialization to send a real Message,
                # accept it transparently.
                if "content" in resp:
                    return _unwrap_response(resp)
                final_text = json.dumps(resp, ensure_ascii=False)
                looks_like_json = True
            elif isinstance(resp, str):
                final_text = resp
                if final_text and final_text.lstrip()[:1] in "{[":
                    looks_like_json = True
            usage = ev.get("usage") or usage

        elif status == "error":
            proxy_error = ev.get("message") or str(ev)

    if proxy_error:
        log.error("Proxy reported error in stream: %s", proxy_error)
        return None

    body = final_text if final_text is not None else streamed
    if not body:
        return None

    if looks_like_json:
        log.warning(
            "Proxy delivered a JSON-shaped response (likely a tool_use whose "
            "structure the proxy stripped). The agent will not be able to "
            "invoke the tool because the name/id are lost. Returning as text. "
            "Sample: %r", body[:200],
        )

    return {
        "id": f"msg_{(task_id or uuid.uuid4().hex)[:24]}",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4",
        "content": [{"type": "text", "text": body}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage or {},
    }


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def _open_upstream_stream(session: ClientSession, task_id: str, headers: dict):
    """
    Open a GET to the upstream stream URL with a short retry on 404. Some
    proxies need a moment between accepting the task and exposing the
    stream endpoint. Returns the response object (caller must close).
    """
    stream_url = PROXY_STREAM_URL_TEMPLATE.format(task_id=task_id)
    delay = 0.25
    last_resp = None
    for attempt in range(BRIDGE_STREAM_OPEN_RETRIES + 1):
        resp = await session.get(stream_url, headers=headers)
        if resp.status == 200:
            return resp
        # On 404/425 the task may not be ready yet; brief backoff and retry.
        if resp.status in (404, 425) and attempt < BRIDGE_STREAM_OPEN_RETRIES:
            await resp.release()
            log.debug("stream GET %s -> %d (attempt %d), backing off %ss",
                      stream_url, resp.status, attempt + 1, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 3.0)
            continue
        last_resp = resp
        return resp
    return last_resp


def _proxy_headers(request: web.Request) -> dict:
    headers = {
        "Authorization": f"Bearer {PROXY_AUTH_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }
    for h in ("anthropic-version", "anthropic-beta", "x-stainless-arch", "x-stainless-os"):
        if h in request.headers:
            headers[h] = request.headers[h]
    return headers


async def messages_handler(request: web.Request) -> web.StreamResponse:
    """Fake Anthropic /v1/messages endpoint."""
    path_qs = request.path_qs

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid JSON"}},
            status=400,
        )

    is_streaming = bool(body.get("stream", False))

    forward_body = dict(body)
    stripped = [f for f in BRIDGE_STRIP_FIELDS if forward_body.pop(f, None) is not None]
    if stripped:
        log.debug("Stripped fields from forwarded body: %s", stripped)

    # Always request streaming from the upstream proxy. Its GET stream URL is
    # set up for tasks created with stream=true; non-streaming tasks return
    # an empty/non-SSE body that we can't parse. If the original client asked
    # for non-streaming, we'll collect the SSE events back into a single
    # Message dict on our side before responding.
    forward_body["stream"] = True

    upstream_url = f"{PROXY_UPSTREAM_URL}{path_qs}"
    base_headers = _proxy_headers(request)

    log.info("→ POST %s  client_stream=%s (forcing upstream stream=True)", upstream_url, is_streaming)
    started_at = time.monotonic()

    # One session covers BOTH the enqueue POST and the SSE GET. The total
    # client-side timeout is BRIDGE_TIMEOUT; the sock_connect/sock_read
    # timeouts mean we don't hang indefinitely on quiet streams.
    timeout = ClientTimeout(total=BRIDGE_TIMEOUT, connect=30, sock_read=BRIDGE_TIMEOUT)

    async with ClientSession(timeout=timeout) as session:
        # 1. Enqueue the request.
        try:
            async with session.post(upstream_url, json=forward_body, headers=base_headers) as resp:
                upstream_status = resp.status
                ct = resp.headers.get("Content-Type", "")
                if upstream_status == 200:
                    # Synchronous response from proxy. Either Anthropic JSON or
                    # already an SSE stream — handle both.
                    if ct.startswith("text/event-stream"):
                        return await _pipe_or_collect(resp, request, is_streaming)
                    body_json = await resp.json()
                    anthropic_response = _unwrap_response(body_json)
                    log.info("← 200 sync (%.2fs)", time.monotonic() - started_at)
                    return _build_response(anthropic_response, is_streaming)
                if upstream_status != 202:
                    err_body = await _safe_text(resp)
                    log.error("Proxy POST returned %d: %s", upstream_status, err_body[:500])
                    return web.json_response(
                        {"type": "error", "error": {"type": "api_error",
                         "message": f"Proxy returned HTTP {upstream_status}: {err_body[:300]}"}},
                        status=502,
                    )
                accept_body = await resp.json()
        except Exception as e:
            log.exception("Failed to reach proxy for POST")
            return web.json_response(
                {"type": "error", "error": {"type": "api_error",
                 "message": f"Bridge could not reach proxy: {e}"}},
                status=502,
            )

        task_id = (accept_body.get("task_id")
                   if isinstance(accept_body, dict) else None)
        if not task_id:
            log.error("Proxy 202 missing task_id: %s", str(accept_body)[:300])
            return web.json_response(
                {"type": "error", "error": {"type": "api_error",
                 "message": "Proxy returned 202 without a task_id"}},
                status=502,
            )

        log.info("← 202 (%.2fs)  task_id=%s  opening stream", time.monotonic() - started_at, task_id)

        # 2. Open the upstream SSE stream.
        stream_headers = {"Authorization": f"Bearer {PROXY_AUTH_TOKEN}",
                          "Accept": "text/event-stream"}
        try:
            stream_resp = await _open_upstream_stream(session, task_id, stream_headers)
        except Exception as e:
            log.exception("Failed to open upstream stream for task_id=%s", task_id)
            return web.json_response(
                {"type": "error", "error": {"type": "api_error",
                 "message": f"Bridge could not open stream: {e}"}},
                status=502,
            )

        if stream_resp.status != 200:
            err_body = await _safe_text(stream_resp)
            await stream_resp.release()
            log.error("Upstream stream GET returned %d for task_id=%s: %s",
                      stream_resp.status, task_id, err_body[:300])
            return web.json_response(
                {"type": "error", "error": {"type": "api_error",
                 "message": f"Stream endpoint returned HTTP {stream_resp.status}: {err_body[:200]}"}},
                status=502,
            )

        # Defensive: if the proxy returns JSON directly (not SSE), parse it
        # and return without trying to consume an event stream.
        upstream_ct = stream_resp.headers.get("Content-Type", "").lower()
        try:
            if "application/json" in upstream_ct:
                log.info("Stream GET returned JSON (not SSE) for task_id=%s", task_id)
                upstream_json = await stream_resp.json()
                anthropic_response = _unwrap_response(upstream_json)
                if not isinstance(anthropic_response, dict) or "content" not in anthropic_response:
                    log.error("Stream GET JSON not in Anthropic shape  task_id=%s  payload=%s",
                              task_id, str(upstream_json)[:300])
                    return web.json_response(
                        {"type": "error", "error": {"type": "api_error",
                         "message": "Stream endpoint returned JSON but not an Anthropic Message"}},
                        status=502,
                    )
                return _build_response(anthropic_response, is_streaming)

            return await _pipe_or_collect(stream_resp, request, is_streaming, task_id=task_id, started_at=started_at)
        finally:
            if not stream_resp.closed:
                stream_resp.release()


async def _pipe_or_collect(upstream_resp, client_request: web.Request,
                           is_streaming: bool, *, task_id: str | None = None,
                           started_at: float | None = None) -> web.StreamResponse:
    """
    Read the upstream SSE body, detect its format (canonical Anthropic vs
    the proxy's wrapper envelope), and either:
      - For canonical Anthropic + client_stream=True: pipe bytes through.
      - For everything else: assemble into a Message dict and return it
        either as JSON (client_stream=False) or as synthesized SSE
        (client_stream=True). For the proxy's format, this is the only
        path that produces output the CLI can actually parse — piping the
        wrapper bytes verbatim would deliver gibberish.

    Buffering the upstream body costs the user's first-token latency
    (small in practice since the upstream is fast), but it's the only
    safe path while the proxy doesn't preserve Anthropic structure.
    """
    raw_bytes = await upstream_resp.read()
    text = raw_bytes.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    events_raw = [e for e in text.split("\n\n") if e.strip()]
    fmt = _detect_sse_format(events_raw)

    if started_at is not None:
        log.info("← upstream complete  task_id=%s  fmt=%s  bytes=%d  (%.2fs total)",
                 task_id, fmt, len(raw_bytes), time.monotonic() - started_at)

    if fmt == "anthropic":
        # Canonical Anthropic SSE. For streaming clients, just send the
        # bytes back (already in the right format). For non-streaming
        # clients, parse and assemble.
        if is_streaming:
            client_resp = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
            await client_resp.prepare(client_request)
            try:
                await client_resp.write(raw_bytes)
            except ConnectionResetError:
                log.info("client dropped connection while streaming  task_id=%s", task_id)
            await client_resp.write_eof()
            return client_resp

        message = _collect_anthropic_events(events_raw)
        if not message or "content" not in message:
            return web.json_response(
                {"type": "error", "error": {"type": "api_error",
                 "message": "Anthropic SSE did not produce a complete Message"}},
                status=502,
            )
        return web.json_response(message)

    if fmt == "proxy":
        # Proxy wrapper format. Translate to Anthropic Message and return.
        message = _proxy_events_to_message(events_raw)
        if not message or "content" not in message:
            log.error("Proxy SSE produced no Message  task_id=%s", task_id)
            return web.json_response(
                {"type": "error", "error": {"type": "api_error",
                 "message": "Upstream stream did not produce a complete Message"}},
                status=502,
            )
        return _build_response(message, is_streaming)

    # Unknown format.
    log.error("Unrecognized upstream SSE format  task_id=%s  preview=%r",
              task_id, text[:500])
    return web.json_response(
        {"type": "error", "error": {"type": "api_error",
         "message": "Bridge could not recognize the upstream stream format"}},
        status=502,
    )


def _collect_anthropic_events(events_raw: list[str]) -> Optional[dict]:
    """
    Assemble canonical Anthropic SSE events into a Message dict.
    Pulled out of the previous _collect_sse_to_message so the format
    detector can dispatch to it directly.
    """
    message: Optional[dict] = None
    blocks_by_idx: dict[int, dict] = {}
    ordered_indices: list[int] = []

    for event_text in events_raw:
        ev = _parse_sse_event(event_text)
        if not ev:
            continue
        t = ev.get("type")

        if t == "message_start":
            msg = ev.get("message", {}) or {}
            message = dict(msg)
            message["content"] = []
        elif t == "content_block_start":
            idx = ev.get("index", 0)
            block = dict(ev.get("content_block", {}) or {})
            if block.get("type") == "text" and "text" not in block:
                block["text"] = ""
            if block.get("type") == "tool_use":
                block["_input_json"] = ""
            blocks_by_idx[idx] = block
            if idx not in ordered_indices:
                ordered_indices.append(idx)
        elif t == "content_block_delta":
            idx = ev.get("index", 0)
            block = blocks_by_idx.get(idx)
            if not block:
                continue
            delta = ev.get("delta", {}) or {}
            d_type = delta.get("type")
            if d_type == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif d_type == "input_json_delta":
                block["_input_json"] = block.get("_input_json", "") + delta.get("partial_json", "")
            elif d_type == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif d_type == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
        elif t == "content_block_stop":
            idx = ev.get("index", 0)
            block = blocks_by_idx.get(idx)
            if not block:
                continue
            if block.get("type") == "tool_use":
                raw_json = block.pop("_input_json", "") or ""
                try:
                    block["input"] = json.loads(raw_json) if raw_json.strip() else {}
                except json.JSONDecodeError:
                    block["input"] = {}
        elif t == "message_delta":
            if message is None:
                continue
            delta = ev.get("delta", {}) or {}
            if "stop_reason" in delta:
                message["stop_reason"] = delta["stop_reason"]
            if "stop_sequence" in delta:
                message["stop_sequence"] = delta["stop_sequence"]
            if "usage" in ev:
                message["usage"] = ev["usage"]
        elif t == "message_stop":
            break

    if message is None:
        return None
    message["content"] = [blocks_by_idx[i] for i in ordered_indices if i in blocks_by_idx]
    return message


def _build_response(anthropic_response: dict, is_streaming: bool) -> web.StreamResponse:
    """Used for the sync 200 + JSON fallback path."""
    if not is_streaming:
        return web.json_response(anthropic_response, status=200)
    body = _synthesize_sse(anthropic_response)
    return web.Response(
        body=body, status=200,
        headers={"Content-Type": "text/event-stream; charset=utf-8",
                 "Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


async def _safe_text(resp) -> str:
    try:
        return await resp.text()
    except Exception:
        return "<no body>"


# ---------------------------------------------------------------------------
# Misc endpoints
# ---------------------------------------------------------------------------

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "upstream": PROXY_UPSTREAM_URL,
        "stream_template": PROXY_STREAM_URL_TEMPLATE,
        "timeout": BRIDGE_TIMEOUT,
    })


async def root_probe_handler(request: web.Request) -> web.Response:
    """Some clients HEAD `/` to verify the endpoint is alive."""
    return web.Response(status=200, text="bridge ok")


def make_fake_app() -> web.Application:
    app = web.Application(client_max_size=128 * 1024 * 1024)  # 128 MiB for big PDFs
    app.router.add_post("/v1/messages", messages_handler)
    app.router.add_post("/v1/messages/count_tokens", messages_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/", root_probe_handler)  # also covers HEAD
    return app


async def main() -> None:
    logging.basicConfig(
        level=BRIDGE_LOG_LEVEL,
        format="%(asctime)s [bridge] %(levelname)s %(message)s",
    )

    log.info("=" * 60)
    log.info("bridge starting (SSE-pull model)")
    log.info("  fake Anthropic API    : http://%s:%d", BRIDGE_FAKE_HOST, BRIDGE_FAKE_PORT)
    log.info("  upstream POST URL     : %s", PROXY_UPSTREAM_URL)
    log.info("  upstream stream URL   : %s", PROXY_STREAM_URL_TEMPLATE)
    log.info("  timeout               : %ds", BRIDGE_TIMEOUT)
    log.info("  open-stream retries   : %d", BRIDGE_STREAM_OPEN_RETRIES)
    log.info("  strip fields          : %s", sorted(BRIDGE_STRIP_FIELDS) or "(none)")
    log.info("=" * 60)

    runner = web.AppRunner(make_fake_app())
    await runner.setup()
    site = web.TCPSite(runner, BRIDGE_FAKE_HOST, BRIDGE_FAKE_PORT)
    await site.start()

    log.info("bridge ready")

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
