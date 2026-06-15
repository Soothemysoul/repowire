"""B-2: a ghost singleton holder must not block the real peer's reconnect.

q2ok reconnect-storm root cause: the singleton-occupied check in
``PeerRegistry._build_display_name`` rejected any collision with an ONLINE
record, looking only at ``status``. A *ghost* — a record left ONLINE after its
websocket was dropped from the transport (the B-1 half-open) — therefore blocked
the real singleton peer's reconnect forever, which retried in a tight loop
(observed: 10k+ "Singleton role already online" log lines).

Fix: a singleton name is only truly occupied when the holder has a LIVE
transport. A ghost (ONLINE but ``transport.is_connected==False``) is pruned and
the reconnect is allowed to take over.

Fully isolated: a bare ``WebSocketTransport()`` (in-memory) plus
``AsyncMock(spec=WebSocket)`` sockets, ``PeerRegistry`` over tmpdir persistence.
No live daemon, socket, or tmux (q2ok RELEASE-GATE).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocket

from repowire.config.models import AgentType, Config
from repowire.daemon.peer_registry import PeerRegistry
from repowire.daemon.websocket_transport import WebSocketTransport
from repowire.protocol.peers import PeerRole, PeerStatus


def _make_registry(tmp_path: Path, transport: WebSocketTransport) -> PeerRegistry:
    cfg = Config()
    cfg.daemon.spawn.singleton_roles = ["qa-head"]
    return PeerRegistry(
        config=cfg,
        message_router=MagicMock(),
        transport=transport,
        persistence_path=tmp_path / "sessions.json",
    )


@pytest.mark.asyncio
async def test_ghost_singleton_holder_does_not_block_reconnect(tmp_path):
    """A singleton record that is ONLINE but has no live transport (ghost) must
    be taken over by a reconnecting peer, not rejected — the q2ok storm fix."""
    transport = WebSocketTransport()
    registry = _make_registry(tmp_path, transport)

    ghost_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/x/qa-head",
        role=PeerRole.ORCHESTRATOR,
    )
    # No transport.connect for ghost_id → ONLINE in registry, absent from the
    # transport. This is exactly the half-open ghost.
    assert registry._peers[ghost_id].status == PeerStatus.ONLINE
    assert not transport.is_connected(ghost_id)

    # Reconnect of the same singleton (fresh path, no explicit peer_id) must
    # succeed instead of raising "Singleton role already online".
    new_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/x/qa-head",
        role=PeerRole.ORCHESTRATOR,
    )

    assert registry._peers[new_id].status == PeerStatus.ONLINE
    assert ghost_id not in registry._peers  # ghost pruned on takeover


@pytest.mark.asyncio
async def test_live_singleton_holder_still_rejects_collision(tmp_path):
    """Control: a singleton holder with a LIVE transport still blocks a second
    registration — the guard must only relax for ghosts, not real holders."""
    transport = WebSocketTransport()
    registry = _make_registry(tmp_path, transport)

    live_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/x/qa-head",
        role=PeerRole.ORCHESTRATOR,
    )
    await transport.connect(live_id, AsyncMock(spec=WebSocket))
    assert transport.is_connected(live_id)

    with pytest.raises(ValueError, match="Singleton role already online"):
        await registry.allocate_and_register(
            circle="global",
            backend=AgentType.CLAUDE_CODE,
            path="/x/qa-head",
            role=PeerRole.ORCHESTRATOR,
        )
