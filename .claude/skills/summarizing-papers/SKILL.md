---
name: summarizing-papers
description: Produces detailed summaries of downloaded arXiv papers using their metadata and abstracts. Use this skill when the user asks for summaries, overviews, explanations, or key points of papers that have already been found or downloaded in the current session.
---

# Summarizing Papers

## Data source
Paper metadata (title, authors, abstract, category, published date, arxiv_id, filename) is available in the session state populated by the searching-arxiv skill. Use this metadata — do not attempt to read or parse raw PDF bytes.

## Output format per paper

### [number]. [filename]
**Title:** [full title]

**Authors:** [all author names]

**Category:** [category] | **Published:** [date]

**Summary:** [4-6 sentences: what problem the paper addresses, the proposed method or approach, key findings or contributions, and why it matters. Write in plain language for a technical audience.]

---

## Notes
- If the user's original query was in a non-English language, write summaries in that same language.
- If a paper was found but not successfully downloaded, omit it from the output.
- Do not hallucinate details not present in the abstract or metadata.
