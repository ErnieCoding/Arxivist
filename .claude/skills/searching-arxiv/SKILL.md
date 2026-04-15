---
name: searching-arxiv
description: Searches arXiv for scientific papers matching a user query and downloads the PDFs to the local downloads directory. Use this skill when the user asks to find, search, retrieve, fetch, or download papers, articles, or research from arXiv on any topic, by any author, or in any category.
---

# Searching arXiv

## Tools
- `arxiv:search_arxiv` — searches the arXiv API. Parameters: `query` (string, English keywords only), `max_results` (int, default 10, max 50).
- `arxiv:download_paper` — downloads a PDF. Parameters: `arxiv_id` (string), `pdf_url` (string).
- `arxiv:list_downloads` — lists all PDFs in the downloads directory. No parameters.

## Workflow
1. Parse the user's request. Extract: topic keywords, optional author names, optional max_results count (default 10).
2. If the query is not in English, translate keywords to English before calling `arxiv:search_arxiv` (arXiv only indexes English content).
3. For author names, format as `au:LastName` (surname only). These get appended to the query string.
4. Call `arxiv:search_arxiv`. The tool handles stop-word removal and `all:` prefix formatting automatically — just pass plain keywords.
5. For each paper returned, call `arxiv:download_paper` with the `arxiv_id`.
6. Do not use Bash to sleep or add delays — the tools have built-in rate limiting and exponential backoff.
7. Report what was found and downloaded.

## Notes
- If search returns 0 results, suggest the user try broader or different search terms.
- The tool tracks session state internally. Do not attempt to track papers manually.
