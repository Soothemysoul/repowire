"""Shared display-name helpers used by both spawn and peer_registry."""

from __future__ import annotations

import re
from pathlib import Path

from repowire.config.models import AgentType


def sanitize_folder_name(name: str) -> str:
    """Sanitize an arbitrary folder name for use in a peer display_name.

    Replaces characters not matching [a-zA-Z0-9._-] with hyphens,
    collapses runs, strips leading/trailing hyphens.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "-", name)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    sanitized = sanitized.strip("-")
    return sanitized or "peer"


def build_base_display_name(path: str | None, backend: AgentType | str) -> str:
    """Return the canonical base display name for a peer (without collision suffix).

    Format: ``{sanitized-folder}-{backend.value}``
    This is what the daemon's ``_build_display_name`` assigns when no collision
    exists, and what ``spawn_peer`` must predict so that the spawn-ready waiter
    key matches the WebSocket fire-event key.
    """
    folder = sanitize_folder_name(Path(path).name) if path else "peer"
    backend_str = backend.value if isinstance(backend, AgentType) else str(backend)
    return f"{folder}-{backend_str}"
