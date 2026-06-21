"""beads-rz1g part 4: ws-hook reacts to a refresh signal by dropping a marker.

The ws-hook NEVER restarts mid-turn. On a WS ``refresh`` message (or a stale
handshake epoch) it atomically writes a ``.refresh-pending`` marker next to the
other intentional markers; the stop-hook performs the actual self-restart at a
safe turn boundary. Idempotent (a session already at target_epoch is a no-op)
and degrades to no-op when the role-dir is unknown.
"""

from __future__ import annotations

import json

import pytest

import repowire.hooks.websocket_hook as wh
from repowire.hooks.websocket_hook import handle_message


@pytest.fixture(autouse=True)
def _mark_pane_safe(monkeypatch):
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)


@pytest.fixture
def marker_env(monkeypatch, tmp_path):
    """Point _marker_dir at a tmp tree, fix the role and the loaded epoch."""
    monkeypatch.setattr(wh, "_marker_dir", lambda role: tmp_path / role)
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "backend-worker")
    monkeypatch.setattr(wh, "_get_loaded_epoch", lambda: "loaded-1")
    return tmp_path / "backend-worker" / ".refresh-pending"


class TestRefreshSignal:
    async def test_refresh_message_writes_marker(self, marker_env):
        await handle_message(
            {
                "type": "refresh",
                "target_epoch": "deployed-2",
                "reason": "deploy uksi",
                "scope": "all",
            },
            "%1",
        )
        assert marker_env.exists()
        payload = json.loads(marker_env.read_text())
        assert payload == {
            "target_epoch": "deployed-2",
            "reason": "deploy uksi",
            "scope": "all",
        }

    async def test_refresh_is_idempotent_when_epoch_matches(self, marker_env):
        await handle_message(
            {"type": "refresh", "target_epoch": "loaded-1", "reason": "r", "scope": "all"},
            "%1",
        )
        assert not marker_env.exists()

    async def test_refresh_no_role_is_noop(self, monkeypatch, marker_env):
        monkeypatch.setattr(wh, "_resolve_agent_role", lambda: None)
        await handle_message(
            {"type": "refresh", "target_epoch": "deployed-2", "reason": "r", "scope": "all"},
            "%1",
        )
        assert not marker_env.exists()

    async def test_refresh_scope_defaults_to_workers(self, marker_env):
        await handle_message(
            {"type": "refresh", "target_epoch": "deployed-2", "reason": "r"},
            "%1",
        )
        assert json.loads(marker_env.read_text())["scope"] == "workers"


class TestWriteRefreshPendingAtomic:
    def test_atomic_no_tmp_leftover_and_valid_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wh, "_marker_dir", lambda role: tmp_path / role)
        wh._write_refresh_pending(
            "backend-worker", target_epoch="e", reason="r", scope="workers"
        )
        marker = tmp_path / "backend-worker" / ".refresh-pending"
        tmp = tmp_path / "backend-worker" / ".refresh-pending.tmp"
        assert marker.exists()
        assert not tmp.exists()  # rename consumed the tmp file
        assert json.loads(marker.read_text())["target_epoch"] == "e"


class TestHandleRefreshSignalHandshake:
    def test_handshake_mismatch_marks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wh, "_marker_dir", lambda role: tmp_path / role)
        monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "frontend-head")
        monkeypatch.setattr(wh, "_get_loaded_epoch", lambda: "old")
        assert wh._handle_refresh_signal("new", "reconnect-handshake", "all") is True
        marker = tmp_path / "frontend-head" / ".refresh-pending"
        assert json.loads(marker.read_text())["reason"] == "reconnect-handshake"

    def test_handshake_match_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wh, "_marker_dir", lambda role: tmp_path / role)
        monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "frontend-head")
        monkeypatch.setattr(wh, "_get_loaded_epoch", lambda: "same")
        assert wh._handle_refresh_signal("same", "reconnect-handshake", "all") is False
