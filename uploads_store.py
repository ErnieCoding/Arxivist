"""
Upload storage: writes user-uploaded files into uploads/<file_id>/, extracts
text where applicable, and persists meta.json.

For PDFs, no extraction happens at upload time — the agent reads the original
file directly via its Read tool (which sends the PDF as a document block to
Claude). For DOCX/MD/TXT, we pre-extract to text.md so any reader can use it
without re-parsing.
"""

import json
import logging
import os
import time
import uuid
from typing import Optional

import text_extract

log = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(PROJECT_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

ALLOWED_EXTS = {".pdf", ".docx", ".md", ".txt"}
MAX_BYTES = 25 * 1024 * 1024  # 25 MB

PREVIEW_CHARS = 400


class UploadError(Exception):
    pass


def _parse_mode_for(ext: str) -> str:
    if ext == ".pdf":
        return "pdf-native"
    if ext == ".docx":
        return "extracted"
    return "plain"  # md, txt


def save_upload(file_storage) -> dict:
    """
    Save a Flask FileStorage object to uploads/<file_id>/.

    Returns the meta dict (also written to meta.json) plus a text_preview.
    Raises UploadError on validation failures.
    """
    original_name = file_storage.filename or "unnamed"
    ext = os.path.splitext(original_name)[1].lower()

    if ext == ".doc":
        raise UploadError("Legacy .doc files are not supported. Convert to .docx, .pdf, or .txt first.")
    if ext not in ALLOWED_EXTS:
        raise UploadError(f"Unsupported file type: {ext or '(no extension)'}. Allowed: pdf, docx, md, txt.")

    file_id = uuid.uuid4().hex
    dest_dir = os.path.join(UPLOADS_DIR, file_id)
    os.makedirs(dest_dir, exist_ok=True)

    original_path = os.path.join(dest_dir, f"original{ext}")
    file_storage.save(original_path)

    size = os.path.getsize(original_path)
    if size > MAX_BYTES:
        os.remove(original_path)
        os.rmdir(dest_dir)
        raise UploadError(f"File exceeds {MAX_BYTES // (1024 * 1024)} MB limit.")

    parse_mode = _parse_mode_for(ext)
    char_count: Optional[int] = None
    text_preview = ""

    if parse_mode != "pdf-native":
        try:
            text = text_extract.extract(original_path)
        except Exception as e:
            log.error("Text extraction failed for %s: %s", original_path, e)
            raise UploadError(f"Could not extract text from {original_name}: {e}")

        if text is not None:
            text_path = os.path.join(dest_dir, "text.md")
            with open(text_path, "w", encoding="utf-8") as f:
                f.write(text)
            char_count = len(text)
            text_preview = text[:PREVIEW_CHARS]

    meta = {
        "file_id": file_id,
        "name": original_name,
        "ext": ext,
        "size": size,
        "char_count": char_count,
        "parse_mode": parse_mode,
        "created_at": int(time.time()),
    }

    with open(os.path.join(dest_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return {**meta, "text_preview": text_preview}


def read_meta(file_id: str) -> Optional[dict]:
    """Load meta.json for a given file_id, or None if missing."""
    path = os.path.join(UPLOADS_DIR, file_id, "meta.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
