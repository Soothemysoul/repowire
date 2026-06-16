"""beads-evl: peer-side WS reconnect resilience + pane-warning (Fix A + Fix C).

Covers the unbounded reconnect loop, capped exp backoff + full jitter,
peek-only intentional-marker guard, the supervise() watchdog, and the
tmux pane-warning (no stdin injection).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch  # noqa: F401  (kept for ad-hoc use in tests)

import pytest

import repowire.hooks.websocket_hook as wh


# --- Task 1: capped exponential backoff + full jitter -----------------------


def test_backoff_capped_and_jittered(monkeypatch):
    # full jitter: delay in [0, min(cap, base*2**attempt)]
    monkeypatch.setattr(wh.random, "uniform", lambda a, b: b)  # take upper bound
    assert wh._compute_backoff(attempt=0, cap=30.0, base=1.0) == 1.0
    assert wh._compute_backoff(attempt=3, cap=30.0, base=1.0) == 8.0
    assert wh._compute_backoff(attempt=10, cap=30.0, base=1.0) == 30.0  # capped


def test_backoff_lower_bound_is_zero(monkeypatch):
    monkeypatch.setattr(wh.random, "uniform", lambda a, b: a)  # take lower bound
    assert wh._compute_backoff(attempt=5, cap=30.0, base=1.0) == 0.0


# --- Task 3: peek-only intentional-marker helper ----------------------------


def test_marker_present_peek_does_not_unlink(tmp_path, monkeypatch):
    role_dir = tmp_path / "ops" / "devops-head"
    role_dir.mkdir(parents=True)
    marker = role_dir / ".shutdown-intentional"
    marker.write_text("")
    monkeypatch.setattr(wh, "_marker_dir", lambda role: role_dir)
    assert wh._marker_present("devops-head") is True
    assert marker.exists()  # peek-only — NOT consumed (gateway owns consumption)


def test_marker_present_stale_is_false(tmp_path, monkeypatch):
    import os
    import time

    role_dir = tmp_path / "ops" / "devops-head"
    role_dir.mkdir(parents=True)
    marker = role_dir / ".restart-intentional"
    marker.write_text("")
    old = time.time() - 400  # > 300s max-age
    os.utime(marker, (old, old))
    monkeypatch.setattr(wh, "_marker_dir", lambda role: role_dir)
    assert wh._marker_present("devops-head") is False


def test_marker_present_none_role_is_false():
    assert wh._marker_present(None) is False  # graceful degradation (Task 0)


# --- Task 5: Fix C — tmux pane-warning (no stdin injection) ------------------


def test_pane_warn_set_uses_display_message_not_send_keys(monkeypatch):
    monkeypatch.setattr(wh, "_warn_active", False)
    cmds = []
    monkeypatch.setattr(wh.subprocess, "run", lambda args, **k: cmds.append(args))
    wh._pane_warn_set("%1")
    flat = [" ".join(c) for c in cmds]
    assert any("display-message" in f for f in flat)
    assert all("send-keys" not in f for f in flat)   # NEVER stdin injection
    assert all("display-popup" not in f for f in flat)


def test_pane_warn_clear_resets_indicator(monkeypatch):
    monkeypatch.setattr(wh, "_warn_active", False)
    cmds = []
    monkeypatch.setattr(wh.subprocess, "run", lambda args, **k: cmds.append(args))
    wh._pane_warn_set("%1")
    cmds.clear()
    wh._pane_warn_clear("%1")
    flat = [" ".join(c) for c in cmds]
    assert any("select-pane" in f or "set-option" in f for f in flat)


def test_warn_threshold_constant_present():
    assert isinstance(wh._WARN_AFTER_ATTEMPTS, int) and wh._WARN_AFTER_ATTEMPTS >= 1


# --- Task 2: unbounded reconnect loop + pane-safety guard in main() ----------


@pytest.mark.asyncio
async def test_main_stops_reconnecting_when_pane_unsafe(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    # pane unsafe from the start → main must return 0 without infinite loop
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: False)
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "get_display_name", lambda: "devops-head-claude-code")
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    rc = await asyncio.wait_for(wh.main(), timeout=2.0)
    assert rc == 0


@pytest.mark.asyncio
async def test_main_retries_past_old_50_cap(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "get_display_name", lambda: "devops-head-claude-code")
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(wh, "_pane_warn_set", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_clear", lambda pane_id: None)
    attempts = {"n": 0}

    def _safe(pane_id):
        # stay safe for >50 connect failures, then go unsafe to end the test
        return attempts["n"] < 60

    monkeypatch.setattr(wh, "_is_pane_safe", _safe)

    class _Boom:
        async def __aenter__(self):
            attempts["n"] += 1
            raise OSError("connect refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wh.websockets, "connect", lambda *a, **k: _Boom())
    rc = await asyncio.wait_for(wh.main(), timeout=5.0)
    assert attempts["n"] >= 51  # proves we blew past the old max_attempts=50
    assert rc == 0


# --- Task 4: supervise() watchdog outer loop --------------------------------


def test_supervise_respawns_on_crash_when_safe(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "devops-head")
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_marker_present", lambda role: False)
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    calls = {"n": 0}

    def _fake_run(coro):
        coro.close()
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash")   # first run crashes
        return 0                          # second run exits clean → stop

    monkeypatch.setattr(wh.asyncio, "run", _fake_run)
    rc = wh.supervise()
    assert calls["n"] == 2  # respawned exactly once after the crash
    assert rc == 0


def test_supervise_no_respawn_on_intentional_marker(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "devops-head")
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_marker_present", lambda role: True)  # intentional!
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    calls = {"n": 0}

    def _fake_run(coro):
        coro.close()
        calls["n"] += 1
        raise RuntimeError("crash")

    monkeypatch.setattr(wh.asyncio, "run", _fake_run)
    rc = wh.supervise()
    assert calls["n"] == 1  # crashed once, marker present → NO respawn
    assert rc == 1


def test_supervise_no_respawn_when_pane_unsafe(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "devops-head")
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: False)  # Claude gone
    monkeypatch.setattr(wh, "_marker_present", lambda role: False)
    calls = {"n": 0}

    def _fake_run(coro):
        coro.close()
        calls["n"] += 1
        raise RuntimeError("crash")

    monkeypatch.setattr(wh.asyncio, "run", _fake_run)
    rc = wh.supervise()
    assert calls["n"] == 1  # pane unsafe → no respawn
