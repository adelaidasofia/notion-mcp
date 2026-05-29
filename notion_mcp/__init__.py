"""notion-mcp — FastMCP server for the Notion API (v1).

Local-first FastMCP server giving Claude read + write access to Notion pages,
blocks, databases, users, and comments. Reads execute directly; every write is
staged behind draft+confirm (nothing mutates a workspace until
``confirm_change(draft_id)`` fires).

Notion-specific: every request carries the ``Notion-Version`` header (default
``2022-06-28``, override via ``NOTION_VERSION``). Writes are synchronous — unlike
Coda there is no async ``requestId`` to poll.

Auth: a single Notion integration token (``NOTION_API_KEY``, alias
``NOTION_TOKEN``), created at https://www.notion.so/my-integrations. Bearer auth,
base ``https://api.notion.com/v1``. The integration must be shared into each page
or database you want it to reach (Notion's per-resource connection model).

Public entry: ``python3 -m notion_mcp.server`` or the ``notion-mcp`` console script.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"


def _load_env_file() -> None:
    """Load env vars from a `.env` (repo) or `~/.claude/notion-mcp/admin.env`
    WITHOUT overriding values already present in the environment.

    fastmcp does not auto-load .env; loading here keeps tool implementations
    env-driven without forcing every caller to source a shell file first.
    The admin.env path (chmod 600, outside the repo) is the canonical secret
    store per the CLAUDE.md credential-storage rule; .env in the repo root is
    the convenience path for local development (gitignored).
    """
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path.home() / ".claude" / "notion-mcp" / "admin.env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


_load_env_file()
