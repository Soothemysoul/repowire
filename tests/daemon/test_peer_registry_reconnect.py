"""Regression tests for PeerRegistry.allocate_and_register reconnect semantics.

Guards the fix for the circle-bypass regression where a peer first registered
with role=AGENT (before spawn-claude.sh started exporting REPOWIRE_PEER_ROLE)
kept that stale role forever across reconnects, blocking cross-circle
notify_peer from director/brain-admin after a daemon restart.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repowire.config.models import AgentType, Config
from repowire.daemon.peer_registry import PeerRegistry
from repowire.protocol.peers import PeerRole


def _make_registry(tmp_path: Path) -> PeerRegistry:
    path = tmp_path / "sessions.json"
    return PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        persistence_path=path,
    )


@pytest.mark.asyncio
async def test_reconnect_updates_role_when_elevated(tmp_path):
    """Peer first registered as AGENT, then reconnected with ORCHESTRATOR,
    must have its role updated. Mirrors the director post-restart scenario."""
    registry = _make_registry(tmp_path)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/director",
        role=PeerRole.AGENT,
    )
    assert registry._peers[peer_id].role == PeerRole.AGENT

    reconnect_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/director",
        role=PeerRole.ORCHESTRATOR,
        peer_id=peer_id,
    )
    assert reconnect_id == peer_id
    assert registry._peers[peer_id].role == PeerRole.ORCHESTRATOR


@pytest.mark.asyncio
async def test_reconnect_updates_role_when_demoted(tmp_path):
    """Downgrading role on reconnect also applies. Callers are trusted; the
    registry mirrors intent rather than keeping the historical maximum."""
    registry = _make_registry(tmp_path)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/admin",
        role=PeerRole.ORCHESTRATOR,
    )

    await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/admin",
        role=PeerRole.AGENT,
        peer_id=peer_id,
    )
    assert registry._peers[peer_id].role == PeerRole.AGENT


@pytest.mark.asyncio
async def test_fresh_registration_preserves_role_argument(tmp_path):
    """Regression guard for the fresh-registration path: the role kwarg must
    still be honored when no existing peer_id matches."""
    registry = _make_registry(tmp_path)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/telegram",
        role=PeerRole.SERVICE,
    )
    assert registry._peers[peer_id].role == PeerRole.SERVICE
