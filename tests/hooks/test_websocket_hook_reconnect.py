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
