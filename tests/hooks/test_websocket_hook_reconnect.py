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
