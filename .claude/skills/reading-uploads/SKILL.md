---
name: reading-uploads
description: Locates and reads user-uploaded files given a file_id, picking the right reading strategy by file type (PDF via the built-in Read tool for native vision-based parsing of images, tables, and layout; DOCX, MD, and TXT via the pre-extracted text.md). Use this skill whenever you need to consume content that the user has attached to the current chat — including from inside creating-dashboards, summarizing-papers, or any other workflow that needs upload contents.
---

# Reading Uploads

## Canonical file layout

Every uploaded file lives in its own directory under `uploads/`:

```
uploads/<file_id>/
  original.<ext>     # the user's file, untouched
  text.md            # extracted text — present only for docx, md, txt
  meta.json          # metadata
```

`meta.json` shape:

```json
{
  "file_id": "<hex>",
  "name": "<original filename>",
  "ext": ".pdf|.docx|.md|.txt",
  "size": <bytes>,
  "char_count": <int or null>,
  "parse_mode": "pdf-native | extracted | plain",
  "created_at": <unix seconds>
}
```

## Reading strategy by parse_mode

Read `uploads/<file_id>/meta.json` first to learn which mode applies.

| parse_mode | What to read | Why |
|---|---|---|
| `pdf-native` | `Read("uploads/<file_id>/original.pdf")` | The Read tool sends the PDF as a document block to Claude, which has native vision for images, tables, and layout. For PDFs over 20 pages, supply a `pages` range and call again for additional ranges. |
| `extracted` | `Read("uploads/<file_id>/text.md")` | DOCX text + tables, already converted to markdown at upload time. |
| `plain` | `Read("uploads/<file_id>/text.md")` | MD / TXT — stored as-is. |

Never read `original.docx` directly — DOCX is a binary format; the markdown
extraction is what you want.

## Language and limitations

- Preserve the source language. Do not translate the content of uploads.
- DOCX images are not extracted in v1. If the user expected a figure to
  appear and it didn't, explain this limitation and suggest re-uploading the
  document as a PDF.

## Notes

- `meta.json` exposes `char_count` so other skills can decide how to chunk
  long inputs. It is `null` for PDFs (we don't pre-extract).
- This skill never modifies upload files.
