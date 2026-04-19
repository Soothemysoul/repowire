"""Unit tests for telegram persistent state (beads-244)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repowire.telegram.state import (
    _NOTIF_MAP_MAX,
    _NOTIF_MAP_TTL,
    append_notif_entry,
    load_state,
    notif_map_to_dict,
    save_state,
    set_active_chat,
)
from repowire.telegram.bot import TelegramPeer


# ---------------------------------------------------------------------------
# state.py unit tests
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "telegram-state.json"
    state = {
        "chats": {"123": {"active_peer": "director-claude-code", "last_selected_at": "2026-01-01T00:00:00+00:00"}},
        "notif_map": [{"tg_msg_id": 1, "notif_id": "notif-abc", "peer": "pm", "excerpt": "hi", "ts": time.time()}],
    }
    save_state(state, p)
    loaded = load_state(p)
    assert loaded["chats"]["123"]["active_peer"] == "director-claude-code"
    assert len(loaded["notif_map"]) == 1
    assert loaded["notif_map"][0]["notif_id"] == "notif-abc"


def test_load_missing_file_returns_empty(tmp_path):
    p = tmp_path / "nonexistent.json"
    state = load_state(p)
    assert state == {"chats": {}, "notif_map": []}


def test_load_corrupt_json_returns_empty(tmp_path):
    p = tmp_path / "telegram-state.json"
    p.write_text("{not valid json")
    state = load_state(p)
    assert state == {"chats": {}, "notif_map": []}


def test_load_drops_entries_older_than_ttl(tmp_path):
    p = tmp_path / "telegram-state.json"
    now = time.time()
    state = {
        "chats": {},
        "notif_map": [
            {"tg_msg_id": 1, "notif_id": "old", "peer": "a", "excerpt": "x", "ts": now - _NOTIF_MAP_TTL - 1},
            {"tg_msg_id": 2, "notif_id": "new", "peer": "b", "excerpt": "y", "ts": now},
        ],
    }
    save_state(state, p)
    loaded = load_state(p)
    assert len(loaded["notif_map"]) == 1
    assert loaded["notif_map"][0]["notif_id"] == "new"


def test_append_notif_entry_bounded_at_200():
    notif_map: list[dict] = []
    for i in range(_NOTIF_MAP_MAX + 1):
        notif_map = append_notif_entry(notif_map, i, f"notif-{i:04d}", "peer", f"text {i}")
    assert len(notif_map) == _NOTIF_MAP_MAX
    # Oldest entry (tg_msg_id=0) must have been evicted
    ids = [e["tg_msg_id"] for e in notif_map]
    assert 0 not in ids
    assert _NOTIF_MAP_MAX in ids


def test_append_notif_fifo_evicts_oldest():
    notif_map = append_notif_entry([], 10, "notif-aaa", "p", "first")
    notif_map = append_notif_entry(notif_map, 20, "notif-bbb", "p", "second")
    assert len(notif_map) == 2
    assert notif_map[0]["tg_msg_id"] == 10
    assert notif_map[1]["tg_msg_id"] == 20


def test_notif_map_to_dict_keyed_by_tg_msg_id():
    notif_map = [
        {"tg_msg_id": 5, "notif_id": "notif-x", "peer": "a", "excerpt": "e", "ts": time.time()},
        {"tg_msg_id": 9, "notif_id": "notif-y", "peer": "b", "excerpt": "f", "ts": time.time()},
    ]
    d = notif_map_to_dict(notif_map)
    assert d[5]["notif_id"] == "notif-x"
    assert d[9]["peer"] == "b"


def test_set_active_chat_updates_peer():
    chats: dict = {}
    updated = set_active_chat(chats, "123", "director-claude-code")
    assert updated["123"]["active_peer"] == "director-claude-code"
    assert "last_selected_at" in updated["123"]


def test_atomic_write_original_unmodified_on_failure(tmp_path):
    """If os.replace raises mid-write, original file must remain intact."""
    p = tmp_path / "telegram-state.json"
    original = {"chats": {"123": {"active_peer": "original"}}, "notif_map": []}
    save_state(original, p)

    with patch("os.replace", side_effect=OSError("simulated disk failure")):
        save_state({"chats": {"123": {"active_peer": "corrupted"}}, "notif_map": []}, p)

    # Original file must still contain the old value
    on_disk = json.loads(p.read_text())
    assert on_disk["chats"]["123"]["active_peer"] == "original"

    # Temp file must have been cleaned up
    leftover = list(tmp_path.glob(".telegram-state-*"))
    assert leftover == [], f"Temp file not cleaned up: {leftover}"


# ---------------------------------------------------------------------------
# TelegramPeer integration: state restored on init
# ---------------------------------------------------------------------------


def _make_peer(tmp_path: Path) -> TelegramPeer:
    return TelegramPeer(
        bot_token="0:fake",
        chat_id="999",
        daemon_url="http://127.0.0.1:8377",
        state_path=tmp_path / "telegram-state.json",
    )


def test_peer_restores_active_peer_from_disk(tmp_path):
    p = tmp_path / "telegram-state.json"
    save_state(
        {"chats": {"999": {"active_peer": "brain-admin-claude-code", "last_selected_at": "2026-01-01T00:00:00+00:00"}},
         "notif_map": []},
        p,
    )
    peer = _make_peer(tmp_path)
    assert peer._reply_target == "brain-admin-claude-code"


def test_peer_restores_notif_map_from_disk(tmp_path):
    p = tmp_path / "telegram-state.json"
    now = time.time()
    save_state(
        {
            "chats": {},
            "notif_map": [
                {"tg_msg_id": 42, "notif_id": "notif-beef", "peer": "pm", "excerpt": "test", "ts": now},
            ],
        },
        p,
    )
    peer = _make_peer(tmp_path)
    assert 42 in peer._tg_msg_to_notif
    assert peer._tg_msg_to_notif[42]["notif_id"] == "notif-beef"


def test_peer_starts_empty_on_corrupt_state(tmp_path):
    p = tmp_path / "telegram-state.json"
    p.write_text("}{broken")
    peer = _make_peer(tmp_path)
    assert peer._reply_target is None
    assert peer._tg_msg_to_notif == {}
