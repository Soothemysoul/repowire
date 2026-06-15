"""B-3 (part 2): periodic pane-liveness sweep loop.

The cheap liveness_tick (5s) only reconciles the WS connection — it cannot tell
whether a *connected* peer's tmux pane is still alive. The lazy pane-ping
(_demote_unsafe_connected_peers) ran only opportunistically (debounced, on HTTP
traffic), so a BUSY peer whose pane died mid-turn could sit BUSY-zombie until
something happened to trigger lazy_repair.

unsafe_sweep_loop runs that pane-ping sweep on a fixed timer (default 30s,
separate from the WS-only liveness_tick so we don't add pane-ping load to the
5s tick), demoting peers whose pane is gone without manual kill_peer.

Isolated: the sweep method is spied; no transport, daemon, or tmux.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from repowire.config.models import Config
from repowire.daemon.peer_registry import PeerRegistry


def _make_registry(tmp_path: Path) -> PeerRegistry:
    return PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        persistence_path=tmp_path / "sessions.json",
    )


@pytest.mark.asyncio
async def test_unsafe_sweep_loop_calls_demote_periodically(tmp_path):
    """The loop must invoke the pane-liveness reaper repeatedly until cancelled."""
    registry = _make_registry(tmp_path)
    registry._demote_unsafe_connected_peers = AsyncMock(return_value=0)

    task = asyncio.create_task(registry.unsafe_sweep_loop(interval_sec=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert registry._demote_unsafe_connected_peers.await_count >= 2


@pytest.mark.asyncio
async def test_unsafe_sweep_loop_survives_a_failing_sweep(tmp_path):
    """A single sweep raising must not kill the loop — it is best-effort."""
    registry = _make_registry(tmp_path)
    calls = {"n": 0}

    async def flaky() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return 0

    registry._demote_unsafe_connected_peers = flaky

    task = asyncio.create_task(registry.unsafe_sweep_loop(interval_sec=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls["n"] >= 2  # kept going after the first raise


def test_unsafe_sweep_interval_default():
    """Config exposes the sweep interval, defaulting to 30s."""
    cfg = Config()
    assert cfg.daemon.unsafe_sweep_interval_sec == 30
