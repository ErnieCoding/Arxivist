---
name: creating-skills
description: Creates new SKILL.md files that teach the agent how to interact with external databases, APIs, or services it has no existing skill for. Use this skill when the user mentions a database or API by name, provides endpoint URLs, auth tokens, or API documentation, or asks to connect to, store data in, retrieve data from, or interact with any external system that has no matching skill in .claude/skills/.
---

# Creating Skills

## When to activate
- User mentions a database or API with no existing skill (e.g., ArangoDB, Chroma, Qdrant, Postgres, Pinecone, a custom API).
- User provides endpoint URLs, base URLs, auth headers, API keys, or request body schemas.
- User asks to "add to", "store in", "retrieve from", or "connect to" a system with no matching skill.
- An existing skill is missing an endpoint the user just described — update it instead of creating a new one.

## Step 1 — Check for existing skills first

Run this before doing anything else:
```bash
ls .claude/skills/
```

If a relevant skill directory already exists, read its SKILL.md:
```bash
cat .claude/skills/<skill-name>/SKILL.md
```

Only proceed to create a new skill if no matching one exists.

## Step 2 — Extract required information

From the user's message, extract:
- **Skill name**: lowercase, hyphens only, descriptive. Format: `<system>-<operation>` using gerund form. Examples: `ingesting-to-arangodb`, `querying-chroma`, `storing-to-qdrant`. Max 64 characters.
- **Base URL**: root URL of the API (e.g., `http://localhost:8529`).
- **Auth scheme**: header name and value format. Prefer reading credentials from environment variables.
- **Endpoints**: method, path, purpose, request body schema, and expected success response for each.
- **Credential env var name**: e.g., `ARANGO_API_KEY`. Instruct the agent to read it at runtime with Bash.

If the base URL or at least one endpoint is missing, ask the user before proceeding. All other fields can be inferred or left as placeholders.

## Step 3 — Write the new SKILL.md

Use the Write built-in to create the file at `.claude/skills/<skill-name>/SKILL.md`.

The new SKILL.md must use this exact template:

```
---
name: <skill-name>
description: <Single-line third-person description. State what this skill does and when to use it. Include the database/service name explicitly so the agent can match it. Max 1024 characters.>
---

# <Skill name in title case>

## Base URL
`<base_url>`

## Authentication
Header: `<header-name>: <value-format>`
Read the credential at runtime:
```bash
echo $<ENV_VAR_NAME>
```
Store the value in a variable and pass it in the `headers` parameter of `api:call_api`.

## Endpoints

### <Operation name>
- **Method:** POST/GET/PUT/DELETE
- **Path:** `<path>`
- **Full URL:** `<base_url><path>`
- **Purpose:** <what this endpoint does>
- **Request body:**
```json
{
  "field_name": "<type and description>"
}
```
- **Success:** HTTP 200/201 with `<example response>`
- **On error:** Report the HTTP status and response body to the user.

<Repeat for each endpoint provided>

## How to call this API

Use `api:call_api` for all requests. Construct calls as:
```
api:call_api(
  url="<full URL>",
  method="<METHOD>",
  headers={"<auth-header>": "<value-from-env>"},
  body={<fields>}
)
```

## Workflow

1. <Step 1>
2. <Step 2>
3. <Step 3>

## Notes
<Any caveats, rate limits, known error codes, or special handling.>
```

## Step 4 — Confirm and execute

After writing the file:
1. Tell the user: "I've created the `<skill-name>` skill. Proceeding with your request now."
2. Immediately use the new skill to complete the original user request — do not wait for the user to ask again.
3. If the original request requires papers to be downloaded first, run the searching-arxiv skill before ingesting.

## Notes
- Skills are plain-text instruction files — never generate Python code or modify tools.py.
- Keep SKILL.md bodies concise. Under 300 lines is ideal.
- If the user provides partial information (e.g., base URL but no endpoints), create a placeholder skill and note what's missing.
