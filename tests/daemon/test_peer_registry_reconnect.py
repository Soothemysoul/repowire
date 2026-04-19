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


@pytest.mark.asyncio
async def test_daemon_restart_refreshes_role_from_reconnect_payload(tmp_path):
    """End-to-end lifecycle: peer first registered as AGENT, daemon restarts
    (in-memory _peers cleared, on-disk mappings survive), peer reconnects
    with an elevated role. The live peer must reflect the reconnect payload's
    role, not whatever was historically persisted.

    This is the director post-restart scenario that motivated beads-2ft.
    Whether the session_id is the same or a freshly-allocated one depends on
    the name-prune logic in ``_build_display_name`` and is intentionally not
    asserted here — the contract the fix guarantees is "live role reflects
    the reconnect kwarg"."""
    registry = _make_registry(tmp_path)

    # Step 1: first registration at role=AGENT (pre-env-var era).
    first_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/director",
        role=PeerRole.AGENT,
    )
    assert registry._peers[first_id].role == PeerRole.AGENT

    # Step 2: simulate daemon restart — clear live peers, keep mappings on disk.
    registry._peers.clear()

    # Step 3: peer reconnects with env now supplying role=ORCHESTRATOR.
    second_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/director",
        role=PeerRole.ORCHESTRATOR,
    )
    # Live peer carries the current role; any mapping that survived the
    # reconnect must also carry it (no stale AGENT left behind anywhere).
    assert registry._peers[second_id].role == PeerRole.ORCHESTRATOR
    assert registry._mappings[second_id].role == PeerRole.ORCHESTRATOR


@pytest.mark.asyncio
async def test_find_or_allocate_mapping_updates_role_on_reuse(tmp_path):
    """Unit-level guard: the mapping-reuse branch of _find_or_allocate_mapping
    must refresh mapping.role. The outer allocate_and_register depends on it
    for the fresh-path-after-restart lifecycle above."""
    registry = _make_registry(tmp_path)

    first = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/svc",
        role=PeerRole.AGENT,
    )
    assert registry._mappings[first[0]].role == PeerRole.AGENT

    # Reuse path: same display_name/circle/backend, different role.
    sid = registry._find_or_allocate_mapping(
        display_name=registry._peers[first[0]].display_name,
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/svc",
        role=PeerRole.SERVICE,
    )
    assert sid == first[0]
    assert registry._mappings[sid].role == PeerRole.SERVICE
