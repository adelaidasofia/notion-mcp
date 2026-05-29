"""Draft+confirm store for Notion writes.

Mirror of coda-mcp / slack-mcp / whatsapp-mcp / godaddy-mcp / cloudflare-dns-mcp:
every write returns a ``draft_id`` describing the exact HTTP request that WILL be
sent; nothing executes until ``confirm_change(draft_id)``.

The store keeps the *structured request* (method, path, params, body) — not an
executor closure. That makes the staged write fully auditable (the audit log can
record the exact request replayed at confirm time) and keeps the store free of
any client/network state.

Threadsafe via a Lock. Drafts expire after 1 hour. Each draft confirms at most
once.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TTL_SECONDS = 3600  # 1 hour


@dataclass
class Draft:
    draft_id: str
    kind: str                       # tool name that staged it, e.g. "create_page"
    summary: dict[str, Any]         # human-readable preview shown before confirm
    method: str                     # HTTP method to replay
    path: str                       # API path to replay
    params: dict[str, Any] | None = None
    json_body: Any = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    consumed: bool = False

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = self.created_at + DEFAULT_TTL_SECONDS


class DraftStore:
    """In-memory draft store with TTL + one-time confirm semantics."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._drafts: dict[str, Draft] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def _gc(self) -> None:
        """Drop expired drafts. Caller must hold the lock."""
        now = time.time()
        dead = [k for k, d in self._drafts.items() if d.expires_at <= now]
        for k in dead:
            self._drafts.pop(k, None)

    def stage(
        self,
        *,
        kind: str,
        summary: dict[str, Any],
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Draft:
        with self._lock:
            self._gc()
            draft_id = "dft_" + secrets.token_urlsafe(12)
            draft = Draft(
                draft_id=draft_id,
                kind=kind,
                summary=summary,
                method=method,
                path=path,
                params=params,
                json_body=json_body,
                expires_at=time.time() + self._ttl,
            )
            self._drafts[draft_id] = draft
            return draft

    def get(self, draft_id: str) -> Draft:
        with self._lock:
            self._gc()
            if draft_id not in self._drafts:
                raise KeyError(f"draft {draft_id} not found or expired")
            return self._drafts[draft_id]

    def consume(self, draft_id: str) -> Draft:
        """Mark a draft consumed and return it for the caller to replay.

        Raises KeyError if missing/expired, ValueError if already consumed. The
        actual HTTP replay happens in the caller (server.confirm_change) so the
        store stays network-free.
        """
        with self._lock:
            self._gc()
            if draft_id not in self._drafts:
                raise KeyError(f"draft {draft_id} not found or expired")
            draft = self._drafts[draft_id]
            if draft.consumed:
                raise ValueError(f"draft {draft_id} already confirmed; stage a new one")
            draft.consumed = True
            return draft

    def cancel(self, draft_id: str) -> bool:
        with self._lock:
            self._gc()
            return self._drafts.pop(draft_id, None) is not None

    def size(self) -> int:
        with self._lock:
            self._gc()
            return len(self._drafts)
