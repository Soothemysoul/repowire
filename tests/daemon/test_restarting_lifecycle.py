"""beads-k1b3 (q3v5 L2): RESTARTING peer lifecycle in the registry.

For the hold-queue to actually catch the restart window, three things must hold:

1. A WS disconnect of a RESTARTING peer must NOT clobber it to OFFLINE — it is
   restarting, not dead, and incoming notifies must keep being held.
2. The respawn's WS-reconnect must reuse the SAME peer_id (so the spool, keyed
   by peer_id, is flushed) — even past the normal 120s reconnect TTL, because
   respawn+resume runs on a longer timescale.
3. A restart that never returns (stuck past the restart-cap) must be demoted to
   OFFLINE and its orphaned spool cleared — a stuck restart is a genuine failure
   and must not be masked as RESTARTING forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from repowire.config.models import AgentType, Config
from repowire.daemon import hold_queue
from repowire.daemon.peer_registry import PeerRegistry
from repowire.daemon.websocket_transport import WebSocketTransport
from repowire.protocol.peers import PeerStatus


def _make_registry(tmp_path, *, connected: bool = False) -> PeerRegistry:
    transport = MagicMock(spec=WebSocketTransport)
    transport.is_connected = MagicMock(return_value=connected)
    router = MagicMock()
    router.send_notification = AsyncMock()
    return PeerRegistry(
        config=Config(),
        message_router=router,
        query_tracker=None,
        transport=transport,
        persistence_path=tmp_path / "sessions.json",
        hold_queue_dir=tmp_path / "holdq",
    )


async def _allocate(reg, *, path="/tmp/pm", circle="dev"):
    peer_id, _name = await reg.allocate_and_register(
        circle=circle, backend=AgentType.CLAUDE_CODE, path=path,
    )
    return peer_id


def test_set_status_restarting_stamps_restarting_since():
    from repowire.daemon.peer_registry import _set_peer_status
    from repowire.protocol.peers import Peer

    peer = Peer(peer_id="p1", display_name="pm", path="/tmp/pm", machine="t",
                status=PeerStatus.ONLINE)
    _set_peer_status(peer, PeerStatus.RESTARTING)
    assert peer.restarting_since is not None
    # leaving RESTARTING clears it
    _set_peer_status(peer, PeerStatus.ONLINE)
    assert peer.restarting_since is None


async def test_mark_disconnected_keeps_restarting(tmp_path):
    """A RESTARTING peer whose WS drops stays RESTARTING (not OFFLINE)."""
    reg = _make_registry(tmp_path)
    pid = await _allocate(reg)
    await reg.update_peer_status(pid, PeerStatus.RESTARTING)

    await reg.mark_disconnected(pid)

    peer = await reg.get_peer(pid)
    assert peer.status is PeerStatus.RESTARTING


async def test_mark_disconnected_demotes_non_restarting(tmp_path):
    """A normal ONLINE peer whose WS drops is demoted to OFFLINE as before."""
    reg = _make_registry(tmp_path)
    pid = await _allocate(reg)
    await reg.mark_disconnected(pid)
    peer = await reg.get_peer(pid)
    assert peer.status is PeerStatus.OFFLINE


async def test_reconnect_reuses_restarting_peer_id(tmp_path):
    """Respawn over WS reuses the RESTARTING peer's id and flips it ONLINE,
    even past the 120s OFFLINE reconnect TTL."""
    reg = _make_registry(tmp_path)
    pid = await _allocate(reg)
    await reg.update_peer_status(pid, PeerStatus.RESTARTING)
    # age the peer well past the OFFLINE reconnect TTL
    peer = await reg.get_peer(pid)
    peer.last_seen = datetime.now(timezone.utc) - timedelta(seconds=600)

    pid2, _name = await reg.allocate_and_register(
        circle="dev", backend=AgentType.CLAUDE_CODE, path="/tmp/pm",
    )
    assert pid2 == pid
    peer = await reg.get_peer(pid)
    assert peer.status is PeerStatus.ONLINE


async def test_stuck_restart_demoted_after_cap_and_spool_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOWIRE_RESTART_PEER_CAP_SEC", "100")
    reg = _make_registry(tmp_path, connected=False)
    pid = await _allocate(reg)
    await reg.update_peer_status(pid, PeerStatus.RESTARTING)
    hold_queue.enqueue(tmp_path / "holdq", pid, {"text": "x"}, now=0.0)

    # age restarting_since past the cap
    peer = await reg.get_peer(pid)
    peer.restarting_since = datetime.now(timezone.utc) - timedelta(seconds=200)

    await reg.liveness_tick()

    peer = await reg.get_peer(pid)
    assert peer.status is PeerStatus.OFFLINE
    assert hold_queue.count(tmp_path / "holdq", pid) == 0


async def test_fresh_restarting_not_demoted_within_cap(tmp_path, monkeypatch):
    """A peer that JUST started restarting is NOT demoted by the liveness tick."""
    monkeypatch.setenv("REPOWIRE_RESTART_PEER_CAP_SEC", "100")
    reg = _make_registry(tmp_path, connected=False)
    pid = await _allocate(reg)
    await reg.update_peer_status(pid, PeerStatus.RESTARTING)

    await reg.liveness_tick()

    peer = await reg.get_peer(pid)
    assert peer.status is PeerStatus.RESTARTING
