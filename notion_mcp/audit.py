"""JSONL audit log + credential sanitizer for notion-mcp.

One line per tool call, 4-field schema per MCP Build Runbook §"Per-call
observability": ``execution_time_ms``, ``io``, ``token_usage``, ``error_class``.

``token_usage`` is always empty (no LLM in the Notion MCP call path) but the
field ships so aggregators across the MCP family see a uniform shape.

``sanitize_error`` lives here (not in client.py) so both the client and the
server import the redactor from one place — matches the coda-mcp / stripe-mcp
convention.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_AUDIT_PATH = Path(os.environ.get(
    "NOTION_MCP_AUDIT_LOG",
    str(Path.home() / ".claude" / "notion-mcp" / "audit.log.jsonl"),
))

ERROR_CLASSES = {
    "none",
    "auth",
    "rate_limit",
    "validation",
    "not_found",
    "conflict",
    "timeout",
    "upstream_error",
    "internal_error",
}

# Stripped from any string before it crosses into model context or the log.
_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[^\s'\"]+", re.IGNORECASE),
    re.compile(r"(?i)Authorization:\s*[^\s'\"]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|pwd)\s*[=:]\s*([^&\s'\"]+)"),
    # Notion integration token shapes (current `ntn_`, legacy `secret_`)
    re.compile(r"ntn_[A-Za-z0-9]{30,}"),
    re.compile(r"secret_[A-Za-z0-9]{30,}"),
    # Anthropic / OpenAI / GitHub / AWS / Stripe / npm key shapes (defense-in-depth)
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk_live_[0-9a-zA-Z]{20,}"),
    re.compile(r"npm_[A-Za-z0-9]{30,}"),
]


def sanitize_error(text: str) -> str:
    """Strip credentials from a string before logging or returning to the model.

    Reference: ``⚙️ Meta/rules/url-input-safety.md`` sanitize_error() pattern.
    """
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(
            lambda m: f"{m.group(1)}=***" if (m.lastindex or 0) >= 2 else "***REDACTED***",
            out,
        )
    return out


def _serializable(obj: Any) -> Any:
    """Best-effort JSON-serializable coercion. Drops bytes/datetimes/objects to str.

    Every string leaf is run through sanitize_error so no credential can land in
    the audit log even if a tool accidentally puts one in its io payload.
    """
    if isinstance(obj, str):
        return sanitize_error(obj)
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serializable(v) for v in obj]
    return str(obj)


def write(
    tool: str,
    execution_time_ms: int,
    io: dict[str, Any],
    error_class: str = "none",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one audit line. Never raises — audit failure must not break a tool call."""
    if error_class not in ERROR_CLASSES:
        error_class = "internal_error"
    record: dict[str, Any] = {
        "ts": int(time.time()),
        "tool": tool,
        "execution_time_ms": int(execution_time_ms),
        "io": _serializable(io),
        "token_usage": {},
        "error_class": error_class,
    }
    if extra:
        record["extra"] = _serializable(extra)
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


@contextmanager
def time_call(tool: str, io_input: dict[str, Any]):
    """Context manager: records timing + error_class automatically.

    Usage::

        with audit.time_call("get_page", {"page_id": p}) as ctx:
            result = do_work()
            ctx["output"] = {"ok": True}          # captured into io.output
            ctx["error_class"] = "none"           # set on the failure paths
    """
    started = time.perf_counter()
    ctx: dict[str, Any] = {"input": io_input, "output": None, "error_class": "none", "extra": None}
    try:
        yield ctx
    except Exception as exc:
        ctx["error_class"] = ctx.get("error_class") or "internal_error"
        ctx["output"] = {"error": sanitize_error(f"{exc.__class__.__name__}: {exc}")}
        raise
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        write(
            tool=tool,
            execution_time_ms=elapsed_ms,
            io={"input": ctx["input"], "output": ctx["output"]},
            error_class=ctx["error_class"],
            extra=ctx.get("extra"),
        )
