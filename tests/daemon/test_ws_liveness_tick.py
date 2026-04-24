"""Regression tests for PeerRegistry.liveness_tick — beads-y66.

Guards the fix for registry.status drifting from transport connection
state. Pre-fix, Pattern C manifested as status=offline while
transport._connections had no WS (inbound notify 503), OR status=online
while transport was empty (stale).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repowire.config.models import AgentType, Config
from repowire.daemon.peer_registry import PeerRegistry
from repowire.protocol.peers import PeerRole, PeerStatus


class FakeTransport:
    """Minimal WebSocketTransport stand-in for liveness_tick tests.

    Exposes the methods liveness_tick actually calls: is_connected.
    ping() is not used by liveness_tick; include a stub only if the
    impl drifts to call it.
    """

    def __init__(self) -> None:
        self._connected: set[str] = set()

    def set_connected(self, peer_id: str, connected: bool) -> None:
        if connected:
            self._connected.add(peer_id)
        else:
            self._connected.discard(peer_id)

    def is_connected(self, peer_id: str) -> bool:
        return peer_id in self._connected


def _make_registry(tmp_path: Path, transport: FakeTransport) -> PeerRegistry:
    path = tmp_path / "sessions.json"
    return PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        transport=transport,  # type: ignore[arg-type]
        persistence_path=path,
    )


@pytest.mark.asyncio
async def test_liveness_tick_demotes_ghost_peer(tmp_path):
    """Peer ONLINE in registry but no WS in transport — tick marks OFFLINE.

    This is the classic Pattern C trigger: registry lying about deliverability.
    """
    transport = FakeTransport()
    registry = _make_registry(tmp_path, transport)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/ghost",
        role=PeerRole.AGENT,
    )
    # Initial status ONLINE per allocate_and_register contract
    assert registry._peers[peer_id].status == PeerStatus.ONLINE
    # Simulate transport without a WS (e.g. silent TCP death)
    assert not transport.is_connected(peer_id)

    await registry.liveness_tick()

    assert registry._peers[peer_id].status == PeerStatus.OFFLINE


@pytest.mark.asyncio
async def test_liveness_tick_promotes_resurrected_peer(tmp_path):
    """Peer OFFLINE in registry but live WS in transport — tick marks ONLINE.

    Race scenario: old handler's finally demoted the peer, new handler's
    connect set up transport but status flip lost to the race. Tick reconciles.
    """
    transport = FakeTransport()
    registry = _make_registry(tmp_path, transport)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/ghost",
        role=PeerRole.AGENT,
    )
    # Force OFFLINE (simulate stale demote)
    await registry.mark_offline(peer_id)
    assert registry._peers[peer_id].status == PeerStatus.OFFLINE
    # Simulate a live WS being in transport (new handler reconnect)
    transport.set_connected(peer_id, True)

    await registry.liveness_tick()

    assert registry._peers[peer_id].status == PeerStatus.ONLINE


@pytest.mark.asyncio
async def test_liveness_tick_is_idempotent_when_consistent(tmp_path):
    """Tick does nothing when registry and transport already agree."""
    transport = FakeTransport()
    registry = _make_registry(tmp_path, transport)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/consistent",
        role=PeerRole.AGENT,
    )
    transport.set_connected(peer_id, True)

    # Both sides agree: ONLINE + connected
    await registry.liveness_tick()
    assert registry._peers[peer_id].status == PeerStatus.ONLINE

    # Run twice — still stable
    await registry.liveness_tick()
    assert registry._peers[peer_id].status == PeerStatus.ONLINE


@pytest.mark.asyncio
async def test_liveness_tick_preserves_busy_when_connected(tmp_path):
    """A BUSY+connected peer stays BUSY after tick (not promoted to ONLINE)."""
    transport = FakeTransport()
    registry = _make_registry(tmp_path, transport)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/busy",
        role=PeerRole.AGENT,
    )
    await registry.update_peer_status(peer_id, PeerStatus.BUSY)
    transport.set_connected(peer_id, True)

    await registry.liveness_tick()

    assert registry._peers[peer_id].status == PeerStatus.BUSY


@pytest.mark.asyncio
async def test_liveness_tick_no_transport_is_noop(tmp_path):
    """If transport is None (unit-test registry), tick is a silent no-op."""
    path = tmp_path / "sessions.json"
    registry = PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        transport=None,
        persistence_path=path,
    )
    # No crash, no exception
    await registry.liveness_tick()
