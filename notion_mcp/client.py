"""Notion API v1 client over httpx (synchronous).

NO official Notion SDK — a thin httpx wrapper preserves the ``error_class`` signal
the 4-field audit log depends on (rate_limit vs auth vs validation), the same
reasoning coda-mcp / stripe-mcp used to skip the vendor SDK.

Auth:       Authorization: Bearer <NOTION_API_KEY>
Version:    Notion-Version: <NOTION_VERSION>  (default 2022-06-28; REQUIRED by Notion)
Base URL:   https://api.notion.com/v1   (pinned; never user-supplied)
Pagination: cursor-based — `start_cursor` + `page_size` (max 100); response carries
            `results`, `has_more`, `next_cursor`. GET endpoints take the cursor as a
            query param; POST endpoints (search, database query) take it in the body.
Rate limit: ~3 requests/sec average. 429 (code `rate_limited`) -> error_class=rate_limit,
            honors `Retry-After`.
Writes:     synchronous — no async requestId/poll (unlike Coda).

SSRF defense-in-depth: every outbound URL crosses mycelium_security
``sanitize_or_raise`` + ``assert_public_ip`` before httpx touches it. The Notion
host is constant, but the guard means a future base-url override can never resolve
to private space. ``follow_redirects=False`` blocks redirect-based SSRF.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import httpx
from mycelium_security import assert_public_ip, sanitize_or_raise

from .audit import sanitize_error

NOTION_API_BASE = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2022-06-28"
DEFAULT_TIMEOUT = 30.0
_USER_AGENT = "notion-mcp/0.1.0 (+https://github.com/adelaidasofia/notion-mcp)"

# Notion's structured error `code` -> stable error_class. Preferred over the bare
# HTTP status because Notion returns 400/404 for several distinct conditions.
_CODE_TO_CLASS = {
    "unauthorized": "auth",
    "restricted_resource": "auth",
    "object_not_found": "not_found",
    "rate_limited": "rate_limit",
    "validation_error": "validation",
    "invalid_json": "validation",
    "invalid_request": "validation",
    "invalid_request_url": "validation",
    "missing_version": "validation",
    "conflict_error": "conflict",
    "internal_server_error": "upstream_error",
    "service_unavailable": "upstream_error",
    "database_connection_unavailable": "upstream_error",
    "gateway_timeout": "timeout",
}


class NotionError(Exception):
    """Raised when Notion returns a non-2xx response (or transport fails)."""

    def __init__(
        self,
        error_class: str,
        http_status: int,
        message: str,
        code: str | None = None,
    ):
        super().__init__(message)
        self.error_class = error_class
        self.http_status = http_status
        self.code = code
        self.message = message


def _classify(status: int, code: str | None = None) -> str:
    """Map a Notion error to a stable error_class. Prefer the structured `code`."""
    if code and code in _CODE_TO_CLASS:
        return _CODE_TO_CLASS[code]
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status in (400, 422):
        return "validation"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status == 408:
        return "timeout"
    if 500 <= status < 600:
        return "upstream_error"
    return "internal_error"


def _guard_url(url: str) -> str:
    """Run the SSRF guard on a URL before httpx touches it."""
    safe = sanitize_or_raise(url)
    host = urlparse(safe).hostname or ""
    assert_public_ip(host)
    return safe


def resolve_token() -> str:
    """The Notion integration token from env. Accepts NOTION_API_KEY or NOTION_TOKEN."""
    return (os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN") or "").strip()


def resolve_version() -> str:
    """The Notion-Version header value (env override, else the pinned default)."""
    return (os.environ.get("NOTION_VERSION") or DEFAULT_NOTION_VERSION).strip()


class NotionClient:
    """Thin synchronous httpx wrapper around the Notion REST API."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = NOTION_API_BASE,
        version: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._token = (token if token is not None else resolve_token()).strip()
        if not self._token:
            raise NotionError(
                "auth",
                0,
                "NOTION_API_KEY is not set. Create an integration at "
                "https://www.notion.so/my-integrations, copy its token, and drop it into "
                "~/.claude/notion-mcp/admin.env (chmod 600) or the env block. Remember to "
                "share each page/database with the integration.",
            )
        self._base_url = base_url.rstrip("/")
        self._version = (version or resolve_version()).strip()
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._version,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Issue one request. Returns the parsed JSON body (or {} for empty 2xx).

        Raises NotionError with a stable error_class on any non-2xx response.
        """
        if not path.startswith("/"):
            path = "/" + path
        url = self._base_url + path
        _guard_url(url)
        cleaned = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                resp = client.request(
                    method.upper(),
                    url,
                    headers=self._headers(),
                    params=cleaned or None,
                    json=json_body,
                )
        except httpx.TimeoutException as exc:
            raise NotionError("timeout", 0, sanitize_error(f"Notion API timeout: {exc}")) from exc
        except httpx.HTTPError as exc:
            raise NotionError("upstream_error", 0, sanitize_error(f"Notion transport error: {exc}")) from exc

        if resp.status_code >= 400:
            body: Any
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {}
            message = ""
            code = None
            if isinstance(body, dict):
                # Notion errors: {"object":"error","status":..,"code":"..","message":".."}
                message = body.get("message") or ""
                code = body.get("code")
            if not message:
                message = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
            raise NotionError(
                _classify(resp.status_code, code),
                resp.status_code,
                sanitize_error(f"Notion {method.upper()} {path} -> {resp.status_code}: {message}"),
                code=code,
            )

        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise NotionError(
                "upstream_error",
                resp.status_code,
                sanitize_error(f"non-JSON response from Notion: {exc}"),
            ) from exc

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def paginate(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        limit: int = 100,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Follow Notion's ``next_cursor`` until ``limit`` items collected or exhausted.

        Notion list responses are ``{"results": [...], "has_more": bool,
        "next_cursor": "..."}``. GET endpoints (block children, users, comments)
        take the cursor + page_size as query params; POST endpoints (search,
        database query) take them in the JSON body. ``limit`` caps the total
        returned across pages; Notion's own per-page max is 100.
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        remaining = max(int(limit), 0)
        base_params = dict(params or {})
        base_body = dict(json_body or {})
        is_post = method.upper() != "GET"
        while remaining > 0:
            ps = min(page_size, remaining, 100)
            if is_post:
                body = dict(base_body)
                body["page_size"] = ps
                if cursor:
                    body["start_cursor"] = cursor
                page = self.request(method, path, json_body=body)
            else:
                page_params = dict(base_params)
                page_params["page_size"] = ps
                if cursor:
                    page_params["start_cursor"] = cursor
                page = self.get(path, params=page_params)
            results = page.get("results", []) if isinstance(page, dict) else []
            if not isinstance(results, list):
                break
            items.extend(results)
            remaining -= len(results)
            has_more = page.get("has_more") if isinstance(page, dict) else False
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if not has_more or not cursor or not results:
                break
        return items
