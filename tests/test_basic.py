"""notion-mcp smoke tests — no network, no real credentials required.

Covers the safety machinery (the part that must never silently break):
  - package imports clean without a token
  - sanitize_error + audit redaction (incl. Notion token shapes)
  - draft+confirm lifecycle (single-use, cancel idempotent)
  - client refuses to construct without a token
  - the Notion-Version header is always sent
  - HTTP-status + Notion `code` -> error_class mapping
  - cursor pagination injects start_cursor into params (GET) vs body (POST)
  - write tools stage a draft WITHOUT performing any network call
  - create_page / create_comment parent validation
  - healthcheck reports auth_failed with no token (no network)
  - the full tool registry is present (22)
"""

from __future__ import annotations

import importlib

import pytest


def _fn(tool):
    """Unwrap a FastMCP-decorated tool to its underlying function."""
    return tool.fn if hasattr(tool, "fn") else tool


def test_package_imports_without_credentials(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    import notion_mcp.server as server
    importlib.reload(server)
    assert server.mcp is not None
    assert server._DRAFTS is not None


def test_sanitize_error_strips_credentials():
    from notion_mcp.audit import sanitize_error

    raw = (
        "Authorization: Bearer abc.def.ghi rejected; token=hunter2; api_key=oops; "
        "ntn_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA; "
        "secret_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB; "
        "sk-ant-api03-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC; "
        "ghp_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
    )
    cleaned = sanitize_error(raw)
    assert "abc.def.ghi" not in cleaned
    assert "hunter2" not in cleaned
    assert "oops" not in cleaned
    assert "ntn_AAAA" not in cleaned
    assert "secret_BBBB" not in cleaned
    assert "sk-ant-api03" not in cleaned
    assert "ghp_DDDD" not in cleaned
    assert "REDACTED" in cleaned or "***" in cleaned


def test_audit_write_redacts_and_tolerates(tmp_path, monkeypatch):
    log = tmp_path / "audit.log.jsonl"
    monkeypatch.setenv("NOTION_MCP_AUDIT_LOG", str(log))
    import notion_mcp.audit as audit
    importlib.reload(audit)

    # a secret hidden in the io payload must not survive into the log
    audit.write("smoke", 5, {"input": {"token": "Bearer s3cr3t-value"}, "output": {"ok": True}}, "none")
    assert log.exists()
    content = log.read_text()
    assert "smoke" in content
    assert "s3cr3t-value" not in content


def test_draft_lifecycle_single_use():
    from notion_mcp.drafts import DraftStore

    store = DraftStore(ttl_seconds=60)
    draft = store.stage(
        kind="create_page",
        summary={"action": "CREATE"},
        method="POST",
        path="/pages",
        json_body={"parent": {}, "properties": {}},
    )
    assert draft.draft_id.startswith("dft_")
    assert store.size() == 1

    consumed = store.consume(draft.draft_id)
    assert consumed.draft_id == draft.draft_id
    assert consumed.method == "POST"
    assert consumed.path == "/pages"

    with pytest.raises(ValueError):
        store.consume(draft.draft_id)  # already consumed


def test_draft_cancel_is_idempotent():
    from notion_mcp.drafts import DraftStore

    store = DraftStore(ttl_seconds=60)
    draft = store.stage(kind="delete_block", summary={}, method="DELETE", path="/blocks/x")
    assert store.cancel(draft.draft_id) is True
    assert store.cancel(draft.draft_id) is False


def test_missing_draft_raises_keyerror():
    from notion_mcp.drafts import DraftStore

    store = DraftStore(ttl_seconds=60)
    with pytest.raises(KeyError):
        store.consume("dft_does_not_exist")


def test_client_requires_token(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    from notion_mcp.client import NotionClient, NotionError

    with pytest.raises(NotionError) as exc_info:
        NotionClient()
    assert exc_info.value.error_class == "auth"


def test_token_alias_accepted(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.setenv("NOTION_TOKEN", "alias-token")
    from notion_mcp.client import resolve_token

    assert resolve_token() == "alias-token"


def test_client_sends_notion_version_header(monkeypatch):
    monkeypatch.delenv("NOTION_VERSION", raising=False)
    from notion_mcp.client import DEFAULT_NOTION_VERSION, NotionClient

    c = NotionClient(token="fake-token")
    headers = c._headers()
    assert headers["Notion-Version"] == DEFAULT_NOTION_VERSION
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["Content-Type"] == "application/json"


def test_notion_version_env_override(monkeypatch):
    monkeypatch.setenv("NOTION_VERSION", "2099-09-09")
    from notion_mcp.client import resolve_version

    assert resolve_version() == "2099-09-09"


def test_classify_status_and_code():
    from notion_mcp.client import _classify

    # bare HTTP status
    assert _classify(401) == "auth"
    assert _classify(403) == "auth"
    assert _classify(429) == "rate_limit"
    assert _classify(400) == "validation"
    assert _classify(404) == "not_found"
    assert _classify(409) == "conflict"
    assert _classify(500) == "upstream_error"
    assert _classify(418) == "internal_error"
    # Notion structured code wins over status
    assert _classify(400, "object_not_found") == "not_found"
    assert _classify(400, "rate_limited") == "rate_limit"
    assert _classify(403, "restricted_resource") == "auth"
    assert _classify(400, "validation_error") == "validation"


def test_paginate_injects_cursor_get_vs_post(monkeypatch):
    from notion_mcp.client import NotionClient

    c = NotionClient(token="fake-token")
    calls: list[tuple] = []

    def fake_request(method, path, *, params=None, json_body=None):
        calls.append((method, dict(params or {}), dict(json_body or {})))
        seen_cursor = (params or {}).get("start_cursor") or (json_body or {}).get("start_cursor")
        if seen_cursor:
            return {"results": [{"id": "b"}], "has_more": False, "next_cursor": None}
        return {"results": [{"id": "a"}], "has_more": True, "next_cursor": "cur1"}

    monkeypatch.setattr(c, "request", fake_request)

    # GET: cursor rides in the query params
    got = c.paginate("/blocks/x/children", method="GET", limit=100)
    assert [r["id"] for r in got] == ["a", "b"]
    assert calls[0][0] == "GET" and "start_cursor" not in calls[0][1]
    assert calls[1][1].get("start_cursor") == "cur1"

    # POST: cursor rides in the JSON body
    calls.clear()
    got2 = c.paginate("/search", method="POST", json_body={"query": "x"}, limit=100)
    assert [r["id"] for r in got2] == ["a", "b"]
    assert calls[0][0] == "POST" and "start_cursor" not in calls[0][2]
    assert calls[1][2].get("start_cursor") == "cur1"


def test_write_tools_stage_draft_without_network(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "fake-token-no-network")
    import notion_mcp.server as server
    importlib.reload(server)
    server._CLIENT = None  # rebuild lazily with the fake token

    out = _fn(server.create_page)(parent_page_id="p-abc", title="Hello")
    assert out["ok"] is True
    assert out["draft_id"].startswith("dft_")
    assert "Nothing has changed yet" in out["note"]
    assert server._DRAFTS.size() >= 1

    # clean up the staged draft so the in-memory store doesn't leak across tests
    assert _fn(server.cancel_draft)(out["draft_id"])["removed"] is True


def test_create_page_parent_validation(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "fake-token-no-network")
    import notion_mcp.server as server
    importlib.reload(server)
    server._CLIENT = None

    # neither parent
    none = _fn(server.create_page)(title="x")
    assert none["ok"] is False and none["error_class"] == "validation"

    # both parents
    both = _fn(server.create_page)(parent_page_id="p", parent_database_id="d", properties={"x": 1})
    assert both["ok"] is False and both["error_class"] == "validation"

    # title with a database parent is rejected (title column name is unknown)
    bad = _fn(server.create_page)(parent_database_id="d", title="x")
    assert bad["ok"] is False and bad["error_class"] == "validation"


def test_create_comment_target_validation(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "fake-token-no-network")
    import notion_mcp.server as server
    importlib.reload(server)
    server._CLIENT = None

    none = _fn(server.create_comment)(text="hi")
    assert none["ok"] is False and none["error_class"] == "validation"

    both = _fn(server.create_comment)(text="hi", parent_page_id="p", discussion_id="d")
    assert both["ok"] is False and both["error_class"] == "validation"


def test_search_filter_validation(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "fake-token-no-network")
    import notion_mcp.server as server
    importlib.reload(server)
    server._CLIENT = None

    out = _fn(server.search)(filter_type="banana")
    assert out["ok"] is False and out["error_class"] == "validation"


def test_healthcheck_without_token_reports_auth_failed(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    import notion_mcp.server as server
    importlib.reload(server)
    server._CLIENT = None

    out = _fn(server.healthcheck)()
    assert out["ok"] is False
    assert out["tier"] == "auth_failed"


def test_all_tools_registered():
    import notion_mcp.server as server

    expected = [
        # read
        "healthcheck", "whoami", "search", "get_page", "get_page_property",
        "get_block", "get_block_children", "get_database", "query_database",
        "list_users", "get_user", "list_comments",
        # write
        "create_page", "update_page", "append_block_children", "update_block",
        "delete_block", "create_database", "update_database", "create_comment",
        # lifecycle
        "confirm_change", "cancel_draft",
    ]
    assert len(expected) == 22
    for name in expected:
        tool = getattr(server, name, None)
        assert tool is not None, f"tool {name} not registered"
        assert callable(_fn(tool)), f"tool {name} has no callable .fn"
