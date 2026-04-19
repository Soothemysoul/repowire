"""Tests for reply-context-prefix plumbing in TelegramPeer."""
from __future__ import annotations

import time

import pytest

from repowire.telegram.bot import (
    _MSG_MAP_MAX,
    _MSG_MAP_TTL,
    _NOTIF_ID_RE,
    TelegramPeer,
)


def _make_peer() -> TelegramPeer:
    return TelegramPeer(bot_token="0:fake", chat_id="123", daemon_url="http://127.0.0.1:8377")


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
