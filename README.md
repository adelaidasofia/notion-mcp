# notion-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Notion](https://notion.so) API (v1). Gives Claude read **and** write access to
your Notion pages, blocks, databases, users, and comments — with a safety model
the other Notion MCPs don't ship.

Built with [FastMCP](https://github.com/jlowin/fastmcp). Python, stdio, local-first.

## What makes it different

Most Notion MCP servers expose the API and execute writes immediately. This one
adds the layer that matters when an agent is driving:

- **Draft + confirm on every write.** `create_page`, `update_page`,
  `append_block_children`, `delete_block`, `create_database`, `create_comment`, …
  none of them touch your workspace directly. They stage a draft and return a
  `draft_id` with a human-readable preview. Nothing mutates until you call
  `confirm_change(draft_id)`. Drafts are single-use and expire after an hour.
- **4-field per-call audit log.** Every call appends one JSONL line
  (`execution_time_ms`, `io`, `token_usage`, `error_class`) to
  `~/.claude/notion-mcp/audit.log.jsonl`, with credentials redacted on every line.
- **Stable `error_class` taxonomy.** `auth` / `rate_limit` / `validation` /
  `not_found` / `conflict` / `timeout` / `upstream_error` — mapped from Notion's
  own structured error `code`, so the model can react to "rate limited"
  differently from "bad token". Direct httpx (no SDK) keeps that signal intact.
- **SSRF-guarded HTTP.** Every outbound URL is checked before the request;
  redirects are not followed.

Notion writes are **synchronous** — a confirmed write returns the created or
updated object directly (no polling). Notion has no hard delete: `delete_block`
and `update_page(archived=True)` move things to the trash and are reversible, so
there's one clean draft+confirm gate, no extra flags.

## Tools (22)

**Read** (execute directly): `healthcheck`, `whoami`, `search`, `get_page`,
`get_page_property`, `get_block`, `get_block_children`, `get_database`,
`query_database`, `list_users`, `get_user`, `list_comments`.

**Write** (stage a draft → `confirm_change`): `create_page`, `update_page`,
`append_block_children`, `update_block`, `delete_block`, `create_database`,
`update_database`, `create_comment`.

**Lifecycle**: `confirm_change`, `cancel_draft`.

`search` is the fastest way to start: it returns the pages and databases your
integration can reach, with their ids.

## A note on access

Notion integrations are **connection-scoped**: a brand-new integration sees
nothing until you share specific pages or databases with it (open a page →
`•••` → *Connections* → add your integration). Sharing a page shares its
subpages. If a call returns `object_not_found`, the integration almost certainly
hasn't been connected to that resource yet.

## Install

```bash
git clone https://github.com/adelaidasofia/notion-mcp.git
cd notion-mcp
uv pip install -e .        # or: pip install -e .
```

## Configure

1. Create an integration at <https://www.notion.so/my-integrations> (New
   integration → Internal → copy the token).
2. Provide it via either an `admin.env` file (kept out of any config) or the
   MCP `env` block:

   ```bash
   mkdir -p ~/.claude/notion-mcp
   printf 'NOTION_API_KEY=your-token-here\n' > ~/.claude/notion-mcp/admin.env
   chmod 600 ~/.claude/notion-mcp/admin.env
   ```

3. Share the pages / databases you want the agent to reach with the integration.
4. Register the server with your MCP client (Claude Code / Claude Desktop). Add
   to your `.mcp.json`:

   ```json
   {
     "mcpServers": {
       "notion": {
         "type": "stdio",
         "command": "python3",
         "args": ["-m", "notion_mcp.server"],
         "env": { "PYTHONPATH": "/path/to/notion-mcp" }
       }
     }
   }
   ```

5. Restart your MCP client, then run `healthcheck`.

See [SETUP.md](SETUP.md) for the full walkthrough.

## Audit log

One JSONL line per call at `~/.claude/notion-mcp/audit.log.jsonl` (override with
`NOTION_MCP_AUDIT_LOG`). Useful for "show me a trace" and for cost/latency review.

## Related MCP servers

Part of a family of FastMCP servers that share the same draft+confirm, audit,
and `error_class` conventions:

- [coda-mcp](https://github.com/adelaidasofia/coda-mcp) — the Coda sibling
- [github-mcp](https://github.com/adelaidasofia/github-mcp)
- [slack-mcp](https://github.com/adelaidasofia/slack-mcp)
- [parse-mcp](https://github.com/adelaidasofia/parse-mcp) — documents → markdown
- [linear-mcp](https://github.com/adelaidasofia/linear-mcp)

## License

MIT — see [LICENSE](LICENSE).

---

Built by Adelaida Diaz-Roa. Full install or team version at diazroa.com.
