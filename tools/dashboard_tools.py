"""
Dashboard MCP server: exposes create_dashboard (persist + register) and
extract_text (generic text extraction for any local doc).
"""

import logging
import os
import sys

from claude_agent_sdk import tool, create_sdk_mcp_server

# Allow importing project-root modules when this file is loaded from tools/
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_THIS_DIR)
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

import dashboards_store
import text_extract

log = logging.getLogger(__name__)


@tool(
    "create_dashboard",
    (
        "Persists a complete HTML dashboard (single self-contained document with inline "
        "<style> and <script>) to disk under a UUID, registers it under the caller's "
        "session, and returns the public URL. Use this only after assembling the full "
        "HTML according to design-system.md. The HTML must start with <!DOCTYPE html>, "
        "include a <style> block, and contain at least one <!-- === SECTION:<slug> === --> "
        "anchor comment."
    ),
    {
        "type": "object",
        "properties": {
            "html": {
                "type": "string",
                "description": "Complete HTML document, including DOCTYPE, head, body, inline <style> and <script>. Must follow design-system.md (palette, naming, marker comments).",
            },
            "session_id": {
                "type": "string",
                "description": "The session_id passed in the agent's system prompt. The dashboard will be registered as this session's active dashboard.",
            },
        },
        "required": ["html", "session_id"],
    },
)
async def create_dashboard(args: dict) -> dict:
    html = args.get("html") or ""
    session_id = (args.get("session_id") or "").strip()

    if not session_id:
        return {"content": [{"type": "text", "text": "Error: session_id is required."}]}
    if not html.strip():
        return {"content": [{"type": "text", "text": "Error: html is empty."}]}

    stripped = html.lstrip()
    problems = []
    if not stripped.lower().startswith("<!doctype html>"):
        problems.append("missing `<!DOCTYPE html>` prefix")
    if "<style" not in stripped.lower():
        problems.append("missing inline `<style>` block")
    if "<!-- === SECTION:" not in stripped:
        problems.append("missing at least one `<!-- === SECTION:<slug> === -->` marker")

    if problems:
        return {
            "content": [{
                "type": "text",
                "text": (
                    "Dashboard rejected — the HTML does not match the design system:\n- "
                    + "\n- ".join(problems)
                    + "\nReview design-system.md and regenerate the document."
                ),
            }]
        }

    try:
        result = dashboards_store.register(session_id, html)
    except Exception as e:
        log.exception("create_dashboard register failed")
        return {"content": [{"type": "text", "text": f"Failed to register dashboard: {e}"}]}

    log.info("create_dashboard registered uuid=%s url=%s", result["uuid"], result["url"])
    return {
        "content": [{
            "type": "text",
            "text": (
                f"Dashboard published.\n"
                f"uuid: {result['uuid']}\n"
                f"url: {result['url']}\n"
                f"title: {result.get('title', '')}\n"
                "Tell the user with a markdown link like: [Open the dashboard]("
                f"{result['url']})."
            ),
        }]
    }


@tool(
    "extract_text",
    (
        "Extracts plain text from a local file (docx, md, txt). Returns text and "
        "char_count. For PDFs do NOT use this — read the PDF directly with the built-in "
        "Read tool so Claude's native vision parses images, tables, and layout."
    ),
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to a local file (relative paths are resolved from the project root).",
            },
        },
        "required": ["path"],
    },
)
async def extract_text(args: dict) -> dict:
    path = args.get("path") or ""
    if not path:
        return {"content": [{"type": "text", "text": "Error: path is required."}]}

    if not os.path.isabs(path):
        path = os.path.join(_PROJECT_DIR, path)

    if not os.path.exists(path):
        return {"content": [{"type": "text", "text": f"File not found: {path}"}]}

    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return {
            "content": [{
                "type": "text",
                "text": (
                    "PDFs are not extracted by this tool. Use the built-in Read tool on "
                    "the PDF path — Claude reads PDFs natively (images, tables, layout). "
                    f"Path: {path}"
                ),
            }]
        }

    try:
        text = text_extract.extract(path)
    except Exception as e:
        log.exception("extract_text failed for %s", path)
        return {"content": [{"type": "text", "text": f"Extraction failed: {e}"}]}

    if text is None:
        return {"content": [{"type": "text", "text": f"No text extracted from {path}"}]}

    char_count = len(text)
    log.info("extract_text %s -> %d chars", path, char_count)
    truncated = char_count > 8000
    body = text[:8000]
    return {
        "content": [{
            "type": "text",
            "text": (
                f"char_count: {char_count}\n"
                f"--- text ---\n{body}"
                + ("\n... [truncated to 8000 chars; re-read in chunks if needed]" if truncated else "")
            ),
        }]
    }


dashboard_server = create_sdk_mcp_server(
    name="dashboard",
    version="1.0.0",
    tools=[create_dashboard, extract_text],
)
