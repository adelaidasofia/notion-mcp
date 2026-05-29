"""notion-mcp — FastMCP server for the Notion API (v1).

Tools (22):

  Read (execute directly):
    healthcheck whoami
    search
    get_page get_page_property
    get_block get_block_children
    get_database query_database
    list_users get_user
    list_comments

  Write (stage a draft; nothing mutates until confirm_change):
    create_page update_page
    append_block_children update_block delete_block
    create_database update_database
    create_comment

  Lifecycle:
    confirm_change cancel_draft

Why draft+confirm on every write: Notion writes mutate live shared workspaces
(pages, blocks, databases, comments). Same pattern coda-mcp / slack-mcp /
whatsapp-mcp / godaddy-mcp / cloudflare-dns-mcp use. One-message intent stages a
draft; nothing leaves the MCP until confirm_change(draft_id).

No second opt-in gate and no daily-USD cap: Notion has no hard-delete (deleting a
block or archiving a page is reversible from the trash) and the API is free.
draft+confirm is the right and sufficient gate for this (hard-state x subscription)
class. The three read tools the `ingest-notion` skill calls by name —
`query_database`, `get_page`, `get_block_children` — ship here so that skill works
once this MCP is connected.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastmcp import FastMCP

from . import audit
from .audit import sanitize_error
from .client import NOTION_API_BASE, NotionClient, NotionError, resolve_token, resolve_version
from .drafts import DraftStore

mcp = FastMCP("notion-mcp")
_DRAFTS = DraftStore()
_CLIENT: NotionClient | None = None


def _client() -> NotionClient:
    """Lazy singleton so import never requires a token (healthcheck explains the gap)."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = NotionClient()
    return _CLIENT


def _seg(value: Any) -> str:
    """URL-encode a path segment. Notion ids are UUIDs (dashed or not); harmless to encode."""
    return quote(str(value), safe="")


def _rich_text(text: str) -> list[dict[str, Any]]:
    """Build a minimal Notion rich-text array from a plain string."""
    return [{"type": "text", "text": {"content": text}}]


def _err(exc: Exception) -> dict[str, Any]:
    """Stable error payload the model can branch on."""
    if isinstance(exc, NotionError):
        return {
            "ok": False,
            "error_class": exc.error_class,
            "status": exc.http_status,
            "code": exc.code,
            "message": sanitize_error(str(exc)),
        }
    return {
        "ok": False,
        "error_class": "internal_error",
        "message": sanitize_error(f"{exc.__class__.__name__}: {exc}"),
    }


def _read(tool: str, io_input: dict[str, Any], call):
    """Run a read tool. ``call(client)`` returns (payload_dict, output_summary).

    Returns ``{"ok": True, **payload_dict}`` or a stable error payload.
    """
    with audit.time_call(tool, io_input) as ctx:
        try:
            payload, summary = call(_client())
            ctx["output"] = summary
            return {"ok": True, **payload}
        except NotionError as exc:
            ctx["error_class"] = exc.error_class
            out = _err(exc)
            ctx["output"] = out
            return out
        except (ValueError, TypeError) as exc:
            ctx["error_class"] = "validation"
            out = {"ok": False, "error_class": "validation", "message": sanitize_error(str(exc))}
            ctx["output"] = out
            return out


def _stage(
    tool: str,
    io_input: dict[str, Any],
    *,
    summary: dict[str, Any],
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
):
    """Stage a write as a draft. Touches the client first so a missing token
    fails NOW with a clear auth error rather than at confirm time."""
    with audit.time_call(tool, io_input) as ctx:
        try:
            _client()  # raises NotionError(auth) if token absent
            draft = _DRAFTS.stage(
                kind=tool,
                summary=summary,
                method=method,
                path=path,
                params=params,
                json_body=json_body,
            )
            ctx["output"] = {"draft_id": draft.draft_id}
            return {
                "ok": True,
                "draft_id": draft.draft_id,
                "preview": draft.summary,
                "expires_at": int(draft.expires_at),
                "note": "Nothing has changed yet. Call confirm_change(draft_id) to execute.",
            }
        except NotionError as exc:
            ctx["error_class"] = exc.error_class
            out = _err(exc)
            ctx["output"] = out
            return out


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
def healthcheck() -> dict[str, Any]:
    """Verify the Notion integration token works. Calls GET /users/me.

    Returns tier=authenticated on success (with the bot user + workspace),
    tier=auth_failed when the token is missing or rejected.
    """
    with audit.time_call("healthcheck", {}) as ctx:
        if not resolve_token():
            ctx["error_class"] = "auth"
            out = {
                "ok": False,
                "tier": "auth_failed",
                "message": "NOTION_API_KEY not set. Create an integration at "
                "https://www.notion.so/my-integrations and add its token to "
                "~/.claude/notion-mcp/admin.env (chmod 600) or the .mcp.json env block. "
                "Share each page/database with the integration so it can reach them.",
                "api_base": NOTION_API_BASE,
                "notion_version": resolve_version(),
            }
            ctx["output"] = out
            return out
        try:
            me = _client().get("/users/me")
            bot = me.get("bot", {}) if isinstance(me, dict) else {}
            out = {
                "ok": True,
                "tier": "authenticated",
                "api_base": NOTION_API_BASE,
                "notion_version": resolve_version(),
                "bot": {
                    "id": me.get("id") if isinstance(me, dict) else None,
                    "name": me.get("name") if isinstance(me, dict) else None,
                    "type": me.get("type") if isinstance(me, dict) else None,
                    "workspace_name": bot.get("workspace_name") if isinstance(bot, dict) else None,
                },
            }
            ctx["output"] = {"ok": True}
            return out
        except NotionError as exc:
            ctx["error_class"] = exc.error_class
            out = _err(exc)
            out["tier"] = "auth_failed" if exc.error_class == "auth" else "unknown"
            ctx["output"] = out
            return out


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Return the Notion integration (bot) user the token belongs to (GET /users/me)."""
    return _read("whoami", {}, lambda c: ({"user": c.get("/users/me")}, {"ok": True}))


@mcp.tool()
def search(
    query: str | None = None,
    filter_type: str | None = None,
    sort_direction: str = "descending",
    limit: int = 50,
) -> dict[str, Any]:
    """Search pages and databases the integration can access (POST /search).

    Args:
        query: Free-text matched against page/database titles. Omit to list everything shared.
        filter_type: "page" or "database" to restrict the object type. Default returns both.
        sort_direction: "descending" (default) or "ascending" by last_edited_time.
        limit: Max results to return (paginated).

    Note: Notion search only returns pages/databases the integration has been
    explicitly shared into.
    """
    if filter_type is not None and filter_type not in ("page", "database"):
        return {"ok": False, "error_class": "validation", "message": "filter_type must be 'page' or 'database'."}
    if sort_direction not in ("ascending", "descending"):
        return {"ok": False, "error_class": "validation", "message": "sort_direction must be 'ascending' or 'descending'."}
    body: dict[str, Any] = {}
    if query:
        body["query"] = query
    if filter_type:
        body["filter"] = {"property": "object", "value": filter_type}
    body["sort"] = {"direction": sort_direction, "timestamp": "last_edited_time"}

    def call(c: NotionClient):
        results = c.paginate("/search", method="POST", json_body=body, limit=limit)
        return {"results": results, "count": len(results)}, {"count": len(results)}

    return _read("search", {"query": query, "filter_type": filter_type, "limit": limit}, call)


@mcp.tool()
def get_page(page_id: str) -> dict[str, Any]:
    """Get a page object: its properties, icon, cover, parent, archived state (GET /pages/{id}).

    Returns property *values*, not page body content — use get_block_children for the body.
    """
    return _read("get_page", {"page_id": page_id}, lambda c: ({"page": c.get(f"/pages/{_seg(page_id)}")}, {"page_id": page_id}))


@mcp.tool()
def get_page_property(page_id: str, property_id: str) -> dict[str, Any]:
    """Get one page property's value (GET /pages/{id}/properties/{property_id}).

    Use for properties too large to inline in get_page (rollups, relations, people,
    long rich-text). Paginated property types return the first page with a
    next_cursor in the payload.
    """
    return _read(
        "get_page_property",
        {"page_id": page_id, "property_id": property_id},
        lambda c: (
            {"property_item": c.get(f"/pages/{_seg(page_id)}/properties/{_seg(property_id)}")},
            {"property_id": property_id},
        ),
    )


@mcp.tool()
def get_block(block_id: str) -> dict[str, Any]:
    """Get one block's metadata + type-specific content (GET /blocks/{id})."""
    return _read("get_block", {"block_id": block_id}, lambda c: ({"block": c.get(f"/blocks/{_seg(block_id)}")}, {"block_id": block_id}))


@mcp.tool()
def get_block_children(block_id: str, limit: int = 100) -> dict[str, Any]:
    """List a block's (or page's) direct child blocks (GET /blocks/{id}/children).

    A page id is a valid block id, so this is how you read a page's body content.
    To walk deeper, call get_block_children again on any child whose ``has_children``
    is true. Paginated.
    """

    def call(c: NotionClient):
        blocks = c.paginate(f"/blocks/{_seg(block_id)}/children", method="GET", limit=limit)
        return {"blocks": blocks, "count": len(blocks)}, {"count": len(blocks)}

    return _read("get_block_children", {"block_id": block_id, "limit": limit}, call)


@mcp.tool()
def get_database(database_id: str) -> dict[str, Any]:
    """Get a database's metadata: title, the property schema, parent, URL (GET /databases/{id})."""
    return _read(
        "get_database",
        {"database_id": database_id},
        lambda c: ({"database": c.get(f"/databases/{_seg(database_id)}")}, {"database_id": database_id}),
    )


@mcp.tool()
def query_database(
    database_id: str,
    filter: dict[str, Any] | None = None,  # noqa: A002 - matches Notion's body key
    sorts: list[dict[str, Any]] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Query a database's rows (pages) with optional filter + sorts (POST /databases/{id}/query).

    Args:
        filter: A Notion filter object, e.g.
            {"property": "Status", "select": {"equals": "Done"}}.
        sorts: A list of sort objects, e.g.
            [{"property": "Due", "direction": "ascending"}].
        limit: Max rows to return (paginated).

    Each result is a page object whose ``properties`` are the row's cell values.
    """
    body: dict[str, Any] = {}
    if filter:
        body["filter"] = filter
    if sorts:
        body["sorts"] = sorts

    def call(c: NotionClient):
        rows = c.paginate(f"/databases/{_seg(database_id)}/query", method="POST", json_body=body, limit=limit)
        return {"results": rows, "count": len(rows)}, {"count": len(rows)}

    return _read("query_database", {"database_id": database_id, "limit": limit}, call)


@mcp.tool()
def list_users(limit: int = 100) -> dict[str, Any]:
    """List the workspace's users (people + bots) (GET /users). Paginated."""

    def call(c: NotionClient):
        users = c.paginate("/users", method="GET", limit=limit)
        return {"users": users, "count": len(users)}, {"count": len(users)}

    return _read("list_users", {"limit": limit}, call)


@mcp.tool()
def get_user(user_id: str) -> dict[str, Any]:
    """Get one user by id (GET /users/{id})."""
    return _read("get_user", {"user_id": user_id}, lambda c: ({"user": c.get(f"/users/{_seg(user_id)}")}, {"user_id": user_id}))


@mcp.tool()
def list_comments(block_id: str, limit: int = 100) -> dict[str, Any]:
    """List unresolved comments on a page or block (GET /comments?block_id=). Paginated.

    ``block_id`` is the page id (for page-level comments) or a block id.
    """

    def call(c: NotionClient):
        comments = c.paginate("/comments", method="GET", params={"block_id": block_id}, limit=limit)
        return {"comments": comments, "count": len(comments)}, {"count": len(comments)}

    return _read("list_comments", {"block_id": block_id, "limit": limit}, call)


# ---------------------------------------------------------------------------
# Write tools — each stages a draft; nothing executes until confirm_change()
# ---------------------------------------------------------------------------


@mcp.tool()
def create_page(
    parent_database_id: str | None = None,
    parent_page_id: str | None = None,
    properties: dict[str, Any] | None = None,
    title: str | None = None,
    children: list[dict[str, Any]] | None = None,
    icon_emoji: str | None = None,
    cover_url: str | None = None,
) -> dict[str, Any]:
    """Stage creating a page. Returns a draft_id; confirm_change executes it.

    Exactly one parent is required:
      - parent_database_id: the new page becomes a ROW in that database. Its
        ``properties`` must match the database's schema (the title column included).
      - parent_page_id: the new page becomes a SUBPAGE. Pass ``title`` for the page
        title (or set properties["title"] yourself).

    Args:
        properties: Notion property-value object. For a database parent this carries
            the row's cells; for a page parent only "title" is meaningful.
        title: Convenience for a page-parent child's title. NOT valid with a
            database parent (the title column name varies — put it in properties).
        children: Optional list of block objects for the page body.
        icon_emoji: e.g. "🍄". cover_url: an external image URL for the cover.
    """
    if bool(parent_database_id) == bool(parent_page_id):
        return {"ok": False, "error_class": "validation", "message": "Pass exactly one of parent_database_id or parent_page_id."}
    props = dict(properties or {})
    if title is not None:
        if parent_database_id:
            return {"ok": False, "error_class": "validation", "message": "title is not valid with a database parent — put the title in `properties` under the database's title column name."}
        props.setdefault("title", {"title": _rich_text(title)})
    if not props and not children:
        return {"ok": False, "error_class": "validation", "message": "Pass properties (and/or children) to create a page."}
    if parent_database_id:
        parent = {"type": "database_id", "database_id": parent_database_id}
    else:
        parent = {"type": "page_id", "page_id": parent_page_id}
    body: dict[str, Any] = {"parent": parent, "properties": props}
    if children:
        body["children"] = children
    if icon_emoji:
        body["icon"] = {"type": "emoji", "emoji": icon_emoji}
    if cover_url:
        body["cover"] = {"type": "external", "external": {"url": cover_url}}
    return _stage(
        "create_page",
        {"parent_database_id": parent_database_id, "parent_page_id": parent_page_id},
        summary={
            "action": "CREATE page",
            "parent": parent,
            "property_keys": list(props.keys()),
            "child_block_count": len(children or []),
        },
        method="POST",
        path="/pages",
        json_body=body,
    )


@mcp.tool()
def update_page(
    page_id: str,
    properties: dict[str, Any] | None = None,
    archived: bool | None = None,
    icon_emoji: str | None = None,
    cover_url: str | None = None,
) -> dict[str, Any]:
    """Stage updating a page's properties, icon, cover, or archived state (PATCH /pages/{id}).

    Args:
        properties: Notion property-value object for the columns/fields to change.
        archived: True moves the page to trash (reversible); False restores it.
        icon_emoji / cover_url: replace the page icon / external cover.
    """
    body: dict[str, Any] = {}
    if properties:
        body["properties"] = properties
    if archived is not None:
        body["archived"] = archived
    if icon_emoji is not None:
        body["icon"] = {"type": "emoji", "emoji": icon_emoji}
    if cover_url is not None:
        body["cover"] = {"type": "external", "external": {"url": cover_url}}
    if not body:
        return {"ok": False, "error_class": "validation", "message": "Pass at least one of properties / archived / icon_emoji / cover_url."}
    return _stage(
        "update_page",
        {"page_id": page_id},
        summary={"action": "UPDATE page", "page_id": page_id, "changes": list(body.keys())},
        method="PATCH",
        path=f"/pages/{_seg(page_id)}",
        json_body=body,
    )


@mcp.tool()
def append_block_children(
    block_id: str,
    children: list[dict[str, Any]],
    after: str | None = None,
) -> dict[str, Any]:
    """Stage appending child blocks to a page or block (PATCH /blocks/{id}/children).

    This is how you add body content (paragraphs, headings, to-dos, etc.) to a page
    — pass the page id as ``block_id``.

    Args:
        children: A list of Notion block objects, e.g.
            [{"object":"block","type":"paragraph",
              "paragraph":{"rich_text":[{"type":"text","text":{"content":"Hi"}}]}}].
        after: Optional id of an existing child to insert after (else appended at end).
    """
    if not isinstance(children, list) or not children:
        return {"ok": False, "error_class": "validation", "message": "children must be a non-empty list of block objects."}
    body: dict[str, Any] = {"children": children}
    if after:
        body["after"] = after
    return _stage(
        "append_block_children",
        {"block_id": block_id, "count": len(children)},
        summary={"action": "APPEND block children", "block_id": block_id, "block_count": len(children)},
        method="PATCH",
        path=f"/blocks/{_seg(block_id)}/children",
        json_body=body,
    )


@mcp.tool()
def update_block(
    block_id: str,
    block: dict[str, Any] | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    """Stage updating a single block's content or archived state (PATCH /blocks/{id}).

    Args:
        block: The type-specific update object, e.g.
            {"paragraph": {"rich_text": [{"type":"text","text":{"content":"new"}}]}}
            or {"to_do": {"checked": true}}.
        archived: True archives (trashes) the block; False restores it.
    """
    body: dict[str, Any] = {}
    if block:
        body.update(block)
    if archived is not None:
        body["archived"] = archived
    if not body:
        return {"ok": False, "error_class": "validation", "message": "Pass a `block` update object and/or archived."}
    return _stage(
        "update_block",
        {"block_id": block_id},
        summary={"action": "UPDATE block", "block_id": block_id, "fields": list(body.keys())},
        method="PATCH",
        path=f"/blocks/{_seg(block_id)}",
        json_body=body,
    )


@mcp.tool()
def delete_block(block_id: str) -> dict[str, Any]:
    """Stage deleting a block (DELETE /blocks/{id}).

    Notion "delete" moves the block to trash — it is reversible (restore from the
    page history / trash), so this is a single draft+confirm gate, no extra flag.
    Deleting a block deletes its children too.
    """
    return _stage(
        "delete_block",
        {"block_id": block_id},
        summary={
            "action": "DELETE (trash) block",
            "block_id": block_id,
            "note": "Moves the block (and its children) to trash. Reversible from page history.",
        },
        method="DELETE",
        path=f"/blocks/{_seg(block_id)}",
    )


@mcp.tool()
def create_database(
    parent_page_id: str,
    title: str,
    properties: dict[str, Any],
    is_inline: bool = False,
) -> dict[str, Any]:
    """Stage creating a database under a page (POST /databases).

    Args:
        title: The database title (plain text).
        properties: The database schema. MUST include exactly one ``title``-type
            property, e.g.
            {"Name": {"title": {}}, "Status": {"select": {"options": []}}}.
        is_inline: True embeds the database inline in the parent page.
    """
    if not isinstance(properties, dict) or not properties:
        return {"ok": False, "error_class": "validation", "message": "properties (the database schema, including one title-type property) is required."}
    body: dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": _rich_text(title) if isinstance(title, str) else title,
        "properties": properties,
    }
    if is_inline:
        body["is_inline"] = True
    return _stage(
        "create_database",
        {"parent_page_id": parent_page_id, "title": title},
        summary={"action": "CREATE database", "parent_page_id": parent_page_id, "title": title, "property_keys": list(properties.keys())},
        method="POST",
        path="/databases",
        json_body=body,
    )


@mcp.tool()
def update_database(
    database_id: str,
    title: str | None = None,
    description: str | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stage updating a database's title, description, or schema (PATCH /databases/{id}).

    Args:
        title / description: plain text (replaces the existing value).
        properties: schema changes. Setting a property to null REMOVES that column
            and deletes all its data across every row — review the preview before
            confirming.
    """
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = _rich_text(title) if isinstance(title, str) else title
    if description is not None:
        body["description"] = _rich_text(description) if isinstance(description, str) else description
    if properties is not None:
        body["properties"] = properties
    if not body:
        return {"ok": False, "error_class": "validation", "message": "Pass at least one of title / description / properties."}
    summary: dict[str, Any] = {"action": "UPDATE database", "database_id": database_id, "changes": list(body.keys())}
    if properties is not None:
        summary["warning"] = "Removing a property (setting it null) deletes that column's data across every row."
        summary["property_keys"] = list(properties.keys())
    return _stage(
        "update_database",
        {"database_id": database_id},
        summary=summary,
        method="PATCH",
        path=f"/databases/{_seg(database_id)}",
        json_body=body,
    )


@mcp.tool()
def create_comment(
    text: str,
    parent_page_id: str | None = None,
    discussion_id: str | None = None,
) -> dict[str, Any]:
    """Stage posting a comment (POST /comments). Outward-facing — collaborators see it.

    Pass exactly one target:
      - parent_page_id: starts a new page-level comment thread.
      - discussion_id: replies into an existing discussion thread.
    """
    if bool(parent_page_id) == bool(discussion_id):
        return {"ok": False, "error_class": "validation", "message": "Pass exactly one of parent_page_id or discussion_id."}
    if not text or not text.strip():
        return {"ok": False, "error_class": "validation", "message": "text must be a non-empty comment body."}
    body: dict[str, Any] = {"rich_text": _rich_text(text)}
    if parent_page_id:
        body["parent"] = {"page_id": parent_page_id}
        target = {"parent_page_id": parent_page_id}
    else:
        body["discussion_id"] = discussion_id
        target = {"discussion_id": discussion_id}
    return _stage(
        "create_comment",
        target,
        summary={"action": "CREATE comment", **target, "preview": text[:140]},
        method="POST",
        path="/comments",
        json_body=body,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@mcp.tool()
def confirm_change(draft_id: str) -> dict[str, Any]:
    """Execute a previously-staged write. This is where the Notion API write happens.

    Until this call, no workspace state has changed. Each draft confirms exactly
    once and expires after 1 hour. Notion writes are synchronous — the result is
    the created/updated object, no polling needed.
    """
    with audit.time_call("confirm_change", {"draft_id": draft_id}) as ctx:
        try:
            draft = _DRAFTS.consume(draft_id)
        except KeyError as exc:
            ctx["error_class"] = "not_found"
            out = {"ok": False, "error_class": "not_found", "message": str(exc)}
            ctx["output"] = out
            return out
        except ValueError as exc:
            ctx["error_class"] = "validation"
            out = {"ok": False, "error_class": "validation", "message": str(exc)}
            ctx["output"] = out
            return out

        try:
            body = _client().request(draft.method, draft.path, params=draft.params, json_body=draft.json_body)
        except NotionError as exc:
            ctx["error_class"] = exc.error_class
            out = _err(exc)
            out["draft_id"] = draft.draft_id
            out["kind"] = draft.kind
            ctx["output"] = out
            return out

        ctx["output"] = {"kind": draft.kind}
        return {"ok": True, "draft_id": draft.draft_id, "kind": draft.kind, "result": body}


@mcp.tool()
def cancel_draft(draft_id: str) -> dict[str, Any]:
    """Drop a staged write without executing it. Idempotent."""
    with audit.time_call("cancel_draft", {"draft_id": draft_id}) as ctx:
        removed = _DRAFTS.cancel(draft_id)
        out = {"ok": True, "removed": removed, "draft_id": draft_id}
        ctx["output"] = out
        return out


def run() -> None:
    """Console-script / module entry point."""
    mcp.run()


if __name__ == "__main__":
    run()
