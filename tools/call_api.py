"""
call_api tool — generic HTTP client for the Two-Tier Capability Injection architecture.
All routing decisions are made by the LLM via SKILL.md instructions.
No API-specific logic lives in this file.
"""

import json
import logging
import urllib.request
import urllib.error

from claude_agent_sdk import tool, create_sdk_mcp_server

log = logging.getLogger(__name__)


@tool(
    "call_api",
    (
        "Makes an authenticated HTTP request to any REST API endpoint. "
        "Use this tool when a SKILL.md describes an API and you need to call one of its endpoints. "
        "Supports GET, POST, PUT, PATCH, DELETE. Returns the HTTP status code and response body."
    ),
    {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full endpoint URL including protocol, host, and path. Example: https://db.local/api/v1/ingest"
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                "description": "HTTP method."
            },
            "headers": {
                "type": "object",
                "description": "HTTP headers as key-value string pairs. Include auth headers here. Example: {\"Authorization\": \"Bearer TOKEN\"}",
                "additionalProperties": {"type": "string"}
            },
            "body": {
                "type": "object",
                "description": "JSON request body as a dict. Automatically serialized. Omit for GET/DELETE requests with no body."
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds. Default: 30.",
                "default": 30
            }
        },
        "required": ["url", "method"]
    }
)
async def call_api(args: dict) -> dict:
    url = args["url"]
    method = args["method"].upper()
    headers = dict(args.get("headers") or {})
    body = args.get("body", None)
    timeout = int(args.get("timeout") or 30)

    if body is not None and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"

    body_bytes = json.dumps(body).encode("utf-8") if body is not None else None

    log.info("call_api %s %s | headers=%s | body=%s bytes",
             method, url, list(headers.keys()), len(body_bytes) if body_bytes else 0)

    req = urllib.request.Request(
        url=url,
        data=body_bytes,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            response_body = resp.read().decode("utf-8", errors="replace")
            log.info("call_api %s %s -> HTTP %d", method, url, status)
            truncated = len(response_body) > 4000
            return {
                "content": [{
                    "type": "text",
                    "text": (
                        f"HTTP {status}\n"
                        f"Response: {response_body[:4000]}"
                        f"{'... [truncated to 4000 chars]' if truncated else ''}"
                    )
                }]
            }

    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        log.error("call_api HTTP error %s %s -> HTTP %d", method, url, e.code)
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"HTTP Error {e.code}: {e.reason}\n"
                    f"Response body: {error_body}"
                )
            }]
        }

    except urllib.error.URLError as e:
        log.error("call_api URL error %s %s -> %s", method, url, e.reason)
        return {
            "content": [{
                "type": "text",
                "text": f"Connection error: {e.reason}. Verify the URL is correct and the server is reachable."
            }]
        }

    except TimeoutError:
        log.error("call_api timeout %s %s (%ds)", method, url, timeout)
        return {
            "content": [{
                "type": "text",
                "text": f"Request timed out after {timeout}s. The server may be unavailable or slow."
            }]
        }


api_server = create_sdk_mcp_server(
    name="api",
    version="1.0.0",
    tools=[call_api],
)
