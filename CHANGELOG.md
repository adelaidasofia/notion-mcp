# Changelog

All notable changes to notion-mcp are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [0.1.0] — 2026-05-29

Initial release. 22 tools over the Notion API v1.

### Read (execute directly)
- `healthcheck`, `whoami`
- `search` (pages + databases, paginated)
- `get_page`, `get_page_property`
- `get_block`, `get_block_children`
- `get_database`, `query_database` (filter + sorts, paginated)
- `list_users`, `get_user`
- `list_comments`

### Write (draft + confirm)
- Pages: `create_page`, `update_page`
- Blocks: `append_block_children`, `update_block`, `delete_block`
- Databases: `create_database`, `update_database`
- Comments: `create_comment`
- Lifecycle: `confirm_change`, `cancel_draft`

### Safety
- Every write stages a draft; nothing mutates a workspace until
  `confirm_change(draft_id)`. 1-hour TTL, single-use confirm.
- No second opt-in gate and no daily-USD cap: Notion has no hard delete (delete /
  archive is reversible from the trash) and the API is free, so draft+confirm is
  the right and sufficient gate for this (hard-state × subscription) class.
- httpx-direct (no SDK) preserves the `error_class` signal, mapped from Notion's
  structured error `code`; `follow_redirects=False`.
- Every outbound URL passes an SSRF guard (`mycelium-security`).
- 4-field per-call audit log (`execution_time_ms`, `io`, `token_usage`, `error_class`),
  JSONL at `~/.claude/notion-mcp/audit.log.jsonl`, with credential redaction on every line.
- Every request carries the `Notion-Version` header (default `2022-06-28`,
  override via `NOTION_VERSION`).

### Compatibility
- Ships the three read tools the `ingest-notion` skill calls by name —
  `query_database`, `get_page`, `get_block_children` — so that skill works once
  this MCP is connected.
