# notion-mcp — Setup

A step-by-step from zero to a working `healthcheck`.

## 1. Create a Notion integration

1. Go to <https://www.notion.so/my-integrations>.
2. **New integration** → give it a name (e.g. "Claude") → **Internal** → submit.
3. Copy the **Internal Integration Secret** (the token). You can re-reveal it
   later from the integration's settings.
4. (Recommended) Under **Capabilities**, grant only what you need. Read content
   + (optionally) Insert/Update content + Comment. The integration's reach is
   bounded by both these capabilities and the pages you share with it.

## 2. Share pages/databases with the integration

This is the step people miss. A fresh integration can see **nothing** until you
connect it to specific content:

- Open a page or database → `•••` (top-right) → **Connections** → add your
  integration. Sharing a page also shares everything nested under it.

If a tool returns `error_class: "not_found"` (`object_not_found`), the
integration just hasn't been connected to that resource yet.

## 3. Install

```bash
git clone https://github.com/adelaidasofia/notion-mcp.git
cd notion-mcp
uv pip install -e .          # or: pip install -e .
```

Python 3.11+ required.

## 4. Provide the token

Two options. Pick one.

**Option A — `admin.env` (keeps the secret out of any config file):**

```bash
mkdir -p ~/.claude/notion-mcp
printf 'NOTION_API_KEY=paste-your-token-here\n' > ~/.claude/notion-mcp/admin.env
chmod 600 ~/.claude/notion-mcp/admin.env
```

The server loads this automatically on startup. `NOTION_TOKEN` is accepted as an
alias.

**Option B — the MCP `env` block** (see step 5): add `"NOTION_API_KEY": "..."`
inside `env`. Simpler, but the token then lives in your `.mcp.json`.

## 5. Register the server

Add an entry to your client's `.mcp.json` (Claude Code: the project or
user-scope file). Point `PYTHONPATH` at wherever you cloned the repo:

```json
{
  "mcpServers": {
    "notion": {
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "notion_mcp.server"],
      "env": {
        "PYTHONPATH": "/path/to/notion-mcp"
      }
    }
  }
}
```

If you installed into a virtualenv, point `command` at that venv's `python`
(e.g. `/path/to/notion-mcp/.venv/bin/python`) so the dependencies resolve.

## 6. Restart your MCP client

MCP tools load at startup. New registrations and code changes only take effect
after a restart.

## 7. Verify

Run the `healthcheck` tool. Success looks like:

```json
{ "ok": true, "tier": "authenticated", "notion_version": "2022-06-28",
  "bot": { "name": "Claude", "type": "bot", "workspace_name": "..." } }
```

If you see `tier: "auth_failed"`, the token is missing or rejected — re-check
steps 1 and 4.

## Working with it

- **Start with `search`.** It returns the pages and databases the integration
  can reach, each with the id the other tools take. (Empty results usually means
  step 2 — nothing's been shared with the integration yet.)
- **Read a page's body** with `get_block_children(page_id)`; recurse into any
  child whose `has_children` is true.
- **Writes are two steps.** A write tool returns a `draft_id` and a preview;
  call `confirm_change(draft_id)` to actually perform it, or `cancel_draft`.
  Writes are synchronous — `confirm_change` returns the created/updated object.
- **`delete_block` and `archived=True` are reversible** (Notion moves things to
  trash), which is why there's no extra opt-in flag.
- **Rate limit.** Notion allows ~3 requests/sec on average. A 429 surfaces as
  `error_class: "rate_limit"`.
- **API version.** Requests send `Notion-Version: 2022-06-28` by default;
  override with `NOTION_VERSION`.

## Audit log

Every call appends one line to `~/.claude/notion-mcp/audit.log.jsonl`
(`execution_time_ms`, `io`, `token_usage`, `error_class`), credentials redacted.
Override the path with `NOTION_MCP_AUDIT_LOG`.
