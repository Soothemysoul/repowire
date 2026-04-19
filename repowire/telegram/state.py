"""Persistent state for the Telegram bot peer.

Survives daemon restarts by storing active conversation and notification map
to ~/.repowire/telegram-state.json. Atomic writes via tempfile + os.replace.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_NOTIF_MAP_MAX = 200      # FIFO cap for persisted notification map entries
_NOTIF_MAP_TTL = 86400    # drop entries older than 24 h on load


def _state_path() -> Path:
    return Path.home() / ".repowire" / "telegram-state.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    """Load state from disk. Returns empty state on missing or corrupt file."""
    p = path or _state_path()
    if not p.exists():
        return {"chats": {}, "notif_map": []}
    try:
        raw = json.loads(p.read_text())
        chats = raw.get("chats", {})
        notif_map = raw.get("notif_map", [])

        # Drop stale notif_map entries on load (TTL guard)
        cutoff = time.time() - _NOTIF_MAP_TTL
        notif_map = [e for e in notif_map if isinstance(e, dict) and e.get("ts", 0) >= cutoff]

        return {"chats": chats, "notif_map": notif_map}
    except (json.JSONDecodeError, OSError, ValueError):
        logger.warning("telegram-state.json corrupt or unreadable — starting with empty state")
        return {"chats": {}, "notif_map": []}


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    """Atomically write state to disk. Logs on failure, never raises."""
    p = path or _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".telegram-state-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        logger.error("Failed to save telegram state", exc_info=True)


def append_notif_entry(
    notif_map: list[dict],
    tg_msg_id: int,
    notif_id: str | None,
    peer: str,
    text: str,
) -> list[dict]:
    """Return a new list with the entry appended, capped at _NOTIF_MAP_MAX (FIFO)."""
    entry: dict[str, Any] = {
        "tg_msg_id": tg_msg_id,
        "notif_id": notif_id,
        "peer": peer,
        "excerpt": text[:120],
        "ts": time.time(),
    }
    updated = notif_map + [entry]
    if len(updated) > _NOTIF_MAP_MAX:
        updated = updated[-_NOTIF_MAP_MAX:]
    return updated


def notif_map_to_dict(notif_map: list[dict]) -> dict[int, dict]:
    """Convert list form (persist) to dict keyed by tg_msg_id (runtime lookup)."""
    return {e["tg_msg_id"]: e for e in notif_map if "tg_msg_id" in e}


def set_active_chat(
    chats: dict[str, Any],
    chat_id: str,
    peer: str,
) -> dict[str, Any]:
    """Return updated chats dict with active_peer set for chat_id."""
    updated = dict(chats)
    updated[chat_id] = {
        "active_peer": peer,
        "last_selected_at": datetime.now(timezone.utc).isoformat(),
    }
    return updated
