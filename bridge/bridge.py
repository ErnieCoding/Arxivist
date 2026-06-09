"""
Sync-to-async bridge for proxies that return 202 + webhook callbacks instead of
synchronous Anthropic API responses.

How it works:

    bundled CLI / Anthropic SDK
        |
        |  POST http://127.0.0.1:9999/v1/messages
        |  (Authorization: Bearer <ignored>)
        v
    ┌─────────────── bridge ──────────────────┐
    | 1. Receive standard Messages API request|
    | 2. Forward to upstream proxy, injecting |
    |    webhook_url that points back to us.  |
    | 3. Receive 202 + task_id from proxy.    |
    | 4. Hold original connection open,       |
    |    awaiting webhook on /webhook/<token>.|
    | 5. Webhook arrives -> resolve future -> |
    |    return body to original caller.      |
    | 6. If client asked stream:true,         |
    |    synthesize SSE chunks from response. |
    └─────────────────────────────────────────┘
        |                                 ^
        |  POST upstream/v1/messages      |  POST /webhook/<token>
        |  + {"webhook_url": ".../<tok>"} |  (from upstream proxy)
        v                                 |
    Real upstream proxy (ai.rndl.ru/proxy/anthropic)

Required env vars:
    PROXY_UPSTREAM_URL          e.g. https://ai.rndl.ru/proxy/anthropic
    PROXY_AUTH_TOKEN            the Bearer token issued by the proxy operator
    BRIDGE_WEBHOOK_PUBLIC_URL   public URL the proxy will POST callbacks to
                                e.g. https://neo.rndl.ru/arxivist/_webhook

Optional env vars:
    BRIDGE_TIMEOUT              seconds to wait for webhook (default 300)
    BRIDGE_FAKE_HOST            host to bind fake API on (default 127.0.0.1)
    BRIDGE_FAKE_PORT            port to bind fake API on (default 9999)
    BRIDGE_WEBHOOK_HOST         host to bind webhook receiver on (default 0.0.0.0)
    BRIDGE_WEBHOOK_PORT         port to bind webhook receiver on (default 9998)
    BRIDGE_LOG_LEVEL            DEBUG|INFO|WARNING|ERROR (default INFO)
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from aiohttp import web, ClientSession, ClientTimeout

log = logging.getLogger("bridge")


def _env_required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Required env var {name} is not set")
    return v.strip()


# Config (read at import time)
PROXY_UPSTREAM_URL = _env_required("PROXY_UPSTREAM_URL").rstrip("/")
PROXY_AUTH_TOKEN = _env_required("PROXY_AUTH_TOKEN")
BRIDGE_WEBHOOK_PUBLIC_URL = _env_required("BRIDGE_WEBHOOK_PUBLIC_URL").rstrip("/")

BRIDGE_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "300"))
BRIDGE_FAKE_HOST = os.environ.get("BRIDGE_FAKE_HOST", "127.0.0.1")
BRIDGE_FAKE_PORT = int(os.environ.get("BRIDGE_FAKE_PORT", "9999"))
BRIDGE_WEBHOOK_HOST = os.environ.get("BRIDGE_WEBHOOK_HOST", "0.0.0.0")
BRIDGE_WEBHOOK_PORT = int(os.environ.get("BRIDGE_WEBHOOK_PORT", "9998"))
BRIDGE_LOG_LEVEL = os.environ.get("BRIDGE_LOG_LEVEL", "INFO").upper()

# In-flight requests: callback_token -> asyncio.Future that resolves with the
# webhook payload. The same process serves both the fake API and the webhook
# receiver, so this dict is the rendezvous point.
_pending: dict[str, asyncio.Future] = {}


def _unwrap_response(body: Any) -> Any:
    """
    The proxy may deliver the Anthropic response wrapped under a key like
    'result', 'data', or 'response'. Find the dict that looks like an
    Anthropic Messages response (has 'content' and 'role').
    """
    if not isinstance(body, dict):
        return body
    if "content" in body and ("role" in body or "model" in body):
        return body
    for key in ("result", "data", "response", "message", "payload"):
        inner = body.get(key)
        if isinstance(inner, dict) and "content" in inner and ("role" in inner or "model" in inner):
            return inner
    # Couldn't find a recognizable Anthropic shape — return as-is and let the
    # caller surface a parse error.
    return body


def _sse_event(event: str, data: dict) -> bytes:
    """Format one Server-Sent Event line block."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _synthesize_sse(message: dict) -> list[bytes]:
    """
    Build an SSE event stream from a complete Anthropic Messages response.

    The bundled CLI expects the canonical streaming sequence:
        message_start -> for each block: content_block_start,
        content_block_delta(s), content_block_stop -> message_delta ->
        message_stop.
    """
    msg_id = message.get("id") or f"msg_{uuid.uuid4().hex}"
    model = message.get("model", "claude-sonnet-4")
    role = message.get("role", "assistant")
    usage = message.get("usage", {}) or {}

    events: list[bytes] = []

    # 1. message_start with no content blocks yet
    events.append(_sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": role,
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": usage,
        },
    }))

    # 2. Each content block: start, delta(s), stop
    for idx, block in enumerate(message.get("content", [])):
        block_type = block.get("type", "text")

        if block_type == "text":
            events.append(_sse_event("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            }))
            events.append(_sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "text_delta", "text": block.get("text", "")},
            }))
            events.append(_sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            }))

        elif block_type == "tool_use":
            events.append(_sse_event("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": {},
                },
            }))
            # Send the entire input as one partial_json delta.
            input_json = json.dumps(block.get("input", {}), ensure_ascii=False)
            events.append(_sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": input_json},
            }))
            events.append(_sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            }))

        else:
            # thinking, redacted_thinking, server_tool_use, etc. — pass through
            events.append(_sse_event("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": block,
            }))
            events.append(_sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            }))

    # 3. message_delta with stop_reason and final usage
    events.append(_sse_event("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": message.get("stop_reason", "end_turn"),
            "stop_sequence": message.get("stop_sequence"),
        },
        "usage": usage,
    }))

    # 4. message_stop
    events.append(_sse_event("message_stop", {"type": "message_stop"}))

    return events


async def messages_handler(request: web.Request) -> web.StreamResponse:
    """
    Fake Anthropic /v1/messages endpoint. Accepts standard Messages API
    requests, forwards them through the proxy, awaits the webhook, and
    returns the response either as JSON or as a synthesized SSE stream.
    """
    path = request.path  # "/v1/messages" or "/v1/messages/count_tokens"

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid JSON"}},
            status=400,
        )

    is_streaming = bool(body.get("stream", False))

    # Create the future BEFORE the upstream call so a fast webhook can't race us.
    callback_token = uuid.uuid4().hex
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending[callback_token] = fut

    webhook_url = f"{BRIDGE_WEBHOOK_PUBLIC_URL}/{callback_token}"

    forward_body = dict(body)
    forward_body["webhook_url"] = webhook_url
    # The proxy doesn't stream. Drop the flag for the upstream call;
    # we'll synthesize SSE on the response side if needed.
    forward_body.pop("stream", None)

    headers = {
        "Authorization": f"Bearer {PROXY_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    # Pass through Anthropic headers the proxy may need.
    for h in ("anthropic-version", "anthropic-beta", "x-stainless-arch", "x-stainless-os"):
        if h in request.headers:
            headers[h] = request.headers[h]

    upstream_url = f"{PROXY_UPSTREAM_URL}{path}"
    log.info("→ POST %s  stream=%s  callback=%s", upstream_url, is_streaming, callback_token)

    started_at = time.monotonic()

    try:
        try:
            async with ClientSession(timeout=ClientTimeout(total=60)) as session:
                async with session.post(upstream_url, json=forward_body, headers=headers) as resp:
                    upstream_status = resp.status
                    try:
                        upstream_body = await resp.json()
                    except Exception:
                        upstream_body = {"_raw": await resp.text()}
        except Exception as e:
            log.exception("Failed to reach upstream proxy")
            return web.json_response(
                {"type": "error", "error": {"type": "api_error", "message": f"Bridge could not reach proxy: {e}"}},
                status=502,
            )

        # Two acceptable paths from the proxy:
        #   200: synchronous response, return immediately.
        #   202: async — wait for the webhook keyed by our callback token.
        if upstream_status == 200:
            anthropic_response = _unwrap_response(upstream_body)
            log.info("← 200 sync (%.2fs)  bytes=%d", time.monotonic() - started_at, len(json.dumps(anthropic_response)))
            return _build_response(anthropic_response, is_streaming)

        if upstream_status != 202:
            log.error("Proxy returned %d: %s", upstream_status, str(upstream_body)[:500])
            return web.json_response(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"Proxy returned HTTP {upstream_status}: {str(upstream_body)[:300]}",
                    },
                },
                status=502,
            )

        # 202: extract task_id (informational only — we route by callback_token)
        task_id = upstream_body.get("task_id") if isinstance(upstream_body, dict) else None
        log.info("← 202 (%.2fs)  task_id=%s  awaiting webhook", time.monotonic() - started_at, task_id)

        # Wait for the webhook
        try:
            webhook_payload = await asyncio.wait_for(fut, timeout=BRIDGE_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("Webhook timeout after %ds  callback=%s  task_id=%s", BRIDGE_TIMEOUT, callback_token, task_id)
            return web.json_response(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"No webhook received within {BRIDGE_TIMEOUT}s",
                    },
                },
                status=504,
            )

        log.info("← webhook (%.2fs total)  callback=%s", time.monotonic() - started_at, callback_token)

        anthropic_response = _unwrap_response(webhook_payload)
        if not isinstance(anthropic_response, dict) or "content" not in anthropic_response:
            log.error("Webhook payload not in Anthropic shape  callback=%s  payload=%s",
                      callback_token, str(webhook_payload)[:500])
            return web.json_response(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": "Webhook payload missing 'content' field",
                    },
                },
                status=502,
            )

        return _build_response(anthropic_response, is_streaming)

    finally:
        _pending.pop(callback_token, None)


def _build_response(anthropic_response: dict, is_streaming: bool) -> web.StreamResponse:
    """Return the Anthropic dict as either a JSON 200 or a synthesized SSE 200."""
    if not is_streaming:
        return web.json_response(anthropic_response, status=200)

    # Build SSE chunks and ship them as one response body. The CLI parses
    # event/data lines from the body — it doesn't require the chunks to be
    # spaced over time.
    chunks = _synthesize_sse(anthropic_response)
    body = b"".join(chunks)
    return web.Response(
        body=body,
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


async def webhook_handler(request: web.Request) -> web.Response:
    """
    Webhook receiver. The proxy POSTs the completed Anthropic response here.
    URL pattern: POST /webhook/<callback_token>
    """
    callback_token = request.match_info["token"]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        log.error("Webhook for %s: invalid JSON body", callback_token)
        return web.json_response({"status": "invalid_json"}, status=400)

    fut = _pending.get(callback_token)
    if fut is None:
        log.warning("Webhook for unknown callback=%s (request may have timed out)", callback_token)
        return web.json_response({"status": "no_pending_request"}, status=200)
    if fut.done():
        log.warning("Webhook for already-resolved callback=%s", callback_token)
        return web.json_response({"status": "already_done"}, status=200)

    fut.set_result(body)
    log.info("✓ webhook  callback=%s  bytes=%d", callback_token, len(json.dumps(body)))
    return web.json_response({"status": "ok"}, status=200)


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "pending": len(_pending),
        "upstream": PROXY_UPSTREAM_URL,
        "webhook_public": BRIDGE_WEBHOOK_PUBLIC_URL,
    })


def make_fake_app() -> web.Application:
    app = web.Application(client_max_size=128 * 1024 * 1024)  # 128 MiB — large PDFs as document blocks
    app.router.add_post("/v1/messages", messages_handler)
    app.router.add_post("/v1/messages/count_tokens", messages_handler)
    app.router.add_get("/health", health_handler)
    return app


def make_webhook_app() -> web.Application:
    app = web.Application(client_max_size=128 * 1024 * 1024)
    app.router.add_post("/webhook/{token}", webhook_handler)
    app.router.add_get("/health", health_handler)
    return app


async def main() -> None:
    logging.basicConfig(
        level=BRIDGE_LOG_LEVEL,
        format="%(asctime)s [bridge] %(levelname)s %(message)s",
    )

    log.info("=" * 60)
    log.info("bridge starting")
    log.info("  fake Anthropic API : http://%s:%d", BRIDGE_FAKE_HOST, BRIDGE_FAKE_PORT)
    log.info("  webhook receiver   : http://%s:%d/webhook/<token>", BRIDGE_WEBHOOK_HOST, BRIDGE_WEBHOOK_PORT)
    log.info("  upstream proxy     : %s", PROXY_UPSTREAM_URL)
    log.info("  webhook public URL : %s/<token>", BRIDGE_WEBHOOK_PUBLIC_URL)
    log.info("  timeout            : %ds", BRIDGE_TIMEOUT)
    log.info("=" * 60)

    fake_runner = web.AppRunner(make_fake_app())
    webhook_runner = web.AppRunner(make_webhook_app())

    await fake_runner.setup()
    await webhook_runner.setup()

    fake_site = web.TCPSite(fake_runner, BRIDGE_FAKE_HOST, BRIDGE_FAKE_PORT)
    webhook_site = web.TCPSite(webhook_runner, BRIDGE_WEBHOOK_HOST, BRIDGE_WEBHOOK_PORT)

    await fake_site.start()
    await webhook_site.start()

    log.info("bridge ready")

    # Run forever (until SIGTERM)
    try:
        await asyncio.Event().wait()
    finally:
        await fake_runner.cleanup()
        await webhook_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
