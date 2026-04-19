"""Tests for reply-context-prefix plumbing in TelegramPeer."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repowire.telegram.bot import (
    _MSG_MAP_MAX,
    _MSG_MAP_TTL,
    _NOTIF_ID_RE,
    TelegramPeer,
)


def _make_peer() -> TelegramPeer:
    return TelegramPeer(bot_token="0:fake", chat_id="123", daemon_url="http://127.0.0.1:8377")


def _http_response(json_payload: dict, status_code: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        text=str(json_payload),
        json=lambda: json_payload,
    )


def test_notif_id_regex_extracts_correlation_id():
    m = _NOTIF_ID_RE.search("[#notif-7e5185b7] Process beads-2ru")
    assert m is not None
    assert m.group(1) == "notif-7e5185b7"


def test_notif_id_regex_no_match_returns_none():
    assert _NOTIF_ID_RE.search("plain text without correlation") is None


def test_trim_msg_map_evicts_expired_entries():
    peer = _make_peer()
    now = time.time()
    peer._tg_msg_to_notif[1] = {"from_peer": "a", "text": "old", "notif_id": None, "ts": now - _MSG_MAP_TTL - 1}
    peer._tg_msg_to_notif[2] = {"from_peer": "b", "text": "fresh", "notif_id": None, "ts": now}
    peer._trim_msg_map()
    assert 1 not in peer._tg_msg_to_notif
    assert 2 in peer._tg_msg_to_notif


def test_trim_msg_map_enforces_cap_oldest_first():
    peer = _make_peer()
    now = time.time()
    for i in range(_MSG_MAP_MAX):
        peer._tg_msg_to_notif[i] = {"from_peer": "x", "text": f"m{i}", "notif_id": None, "ts": now}
    peer._trim_msg_map()
    # Cap enforced: oldest (insertion-order) evicted to make room for one more slot.
    assert len(peer._tg_msg_to_notif) == _MSG_MAP_MAX - 1
    assert 0 not in peer._tg_msg_to_notif
    assert (_MSG_MAP_MAX - 1) in peer._tg_msg_to_notif


# ---------------------------------------------------------------------------
# End-to-end mock of the getUpdates → _on_update → _on_text → _notify pipeline.
# Stubs the httpx layer so no real Telegram or daemon traffic happens. Exercises
# the full code path the real bot runs: outbound notify populates the map with
# the real TG message_id; incoming reply-update is parsed; the reply-context
# prefix is composed and prepended; the forwarded /notify payload carries it.
# ---------------------------------------------------------------------------


class _FakeHttp:
    """Minimal async httpx.AsyncClient shim. Records POST bodies by URL substring."""

    def __init__(self, send_message_id: int) -> None:
        self.send_message_id = send_message_id
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict | None = None, **_: object):
        self.posts.append((url, json or {}))
        if "/sendMessage" in url:
            return _http_response({"ok": True, "result": {"message_id": self.send_message_id}})
        if "/setMessageReaction" in url:
            return _http_response({"ok": True, "result": True})
        if "/notify" in url:
            return _http_response({"ok": True})
        return _http_response({"ok": True, "result": {}})

    async def get(self, *args, **kwargs):
        return _http_response({"ok": True, "result": []})

    async def aclose(self) -> None:  # pragma: no cover
        pass


async def test_reply_to_known_notif_prepends_prefix():
    peer = _make_peer()
    fake = _FakeHttp(send_message_id=42)
    peer._http = fake  # type: ignore[assignment]
    peer._reply_target = "director-claude-code"

    # Outbound notify from daemon: populates the map keyed by TG msg_id=42.
    await peer._on_ws({
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "[#notif-deadbeef] Please pick up beads-2ru urgently before EOD",
    })
    assert 42 in peer._tg_msg_to_notif
    assert peer._tg_msg_to_notif[42]["notif_id"] == "notif-deadbeef"

    # Incoming Telegram update: user hit Reply on message 42 and typed "ok".
    await peer._on_update({
        "update_id": 1,
        "message": {
            "message_id": 99,
            "chat": {"id": "123"},
            "text": "ok",
            "reply_to_message": {"message_id": 42},
        },
    })

    forwards = [body for url, body in fake.posts if "/notify" in url]
    assert len(forwards) == 1
    forwarded_text = forwards[0]["text"]
    assert forwarded_text.startswith('[reply to @director-claude-code notif-deadbeef: "')
    assert forwarded_text.endswith("\nok")
    assert forwards[0]["to_peer"] == "director-claude-code"


async def test_reply_excerpt_truncated_at_120_chars():
    peer = _make_peer()
    fake = _FakeHttp(send_message_id=7)
    peer._http = fake  # type: ignore[assignment]
    peer._reply_target = "pm-claude-code"

    long = "x" * 200
    await peer._on_ws({
        "type": "notify",
        "from_peer": "pm-claude-code",
        "text": f"[#notif-aaaabbbb] {long}",
    })
    await peer._on_update({
        "update_id": 2,
        "message": {
            "message_id": 100,
            "chat": {"id": "123"},
            "text": "reply",
            "reply_to_message": {"message_id": 7},
        },
    })

    forwarded = [b for u, b in fake.posts if "/notify" in u][0]["text"]
    # Prefix must exist, excerpt must end with "..." ellipsis, and the raw body
    # past 120 chars must NOT appear in the excerpt.
    assert '..."]' in forwarded
    assert forwarded.startswith('[reply to @pm-claude-code notif-aaaabbbb: "')
    # Everything inside the quotes is ≤120 chars.
    quoted = forwarded.split('"', 2)[1]
    assert len(quoted.rstrip(".")) <= 120


async def test_standalone_message_has_no_reply_prefix():
    """Regression guard: a message with no reply_to_message field must be
    forwarded verbatim, without any prefix."""
    peer = _make_peer()
    fake = _FakeHttp(send_message_id=1)
    peer._http = fake  # type: ignore[assignment]
    peer._reply_target = "director-claude-code"

    await peer._on_update({
        "update_id": 3,
        "message": {
            "message_id": 101,
            "chat": {"id": "123"},
            "text": "plain standalone",
        },
    })

    forwarded = [b for u, b in fake.posts if "/notify" in u][0]["text"]
    assert "[reply to @" not in forwarded
    assert forwarded == "plain standalone"


async def test_reply_to_unknown_message_id_has_no_prefix():
    """Reply to a bot-message we never saw (e.g. predates restart) must fall
    through as standalone text."""
    peer = _make_peer()
    fake = _FakeHttp(send_message_id=1)
    peer._http = fake  # type: ignore[assignment]
    peer._reply_target = "director-claude-code"

    await peer._on_update({
        "update_id": 4,
        "message": {
            "message_id": 102,
            "chat": {"id": "123"},
            "text": "phantom reply",
            "reply_to_message": {"message_id": 99999},
        },
    })

    forwarded = [b for u, b in fake.posts if "/notify" in u][0]["text"]
    assert "[reply to @" not in forwarded
    assert forwarded == "phantom reply"


async def test_reply_expired_entry_falls_through():
    peer = _make_peer()
    fake = _FakeHttp(send_message_id=1)
    peer._http = fake  # type: ignore[assignment]
    peer._reply_target = "director-claude-code"

    # Pre-seed an entry with a stale timestamp (older than TTL).
    peer._tg_msg_to_notif[55] = {
        "from_peer": "director-claude-code",
        "text": "[#notif-oldoldol] ancient notification",
        "notif_id": "notif-oldoldol",
        "ts": time.time() - _MSG_MAP_TTL - 60,
    }
    await peer._on_update({
        "update_id": 5,
        "message": {
            "message_id": 103,
            "chat": {"id": "123"},
            "text": "too late",
            "reply_to_message": {"message_id": 55},
        },
    })

    forwarded = [b for u, b in fake.posts if "/notify" in u][0]["text"]
    assert "[reply to @" not in forwarded
    assert forwarded == "too late"
