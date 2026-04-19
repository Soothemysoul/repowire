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


# ---------------------------------------------------------------------------
# Guards for role=None (caller didn't specify). These exist because an earlier
# iteration of the fix blindly overwrote stored role with the WS handler's
# default AGENT whenever an old hook reconnected without sending role — which
# silently demoted brain-admin from SERVICE to AGENT on the first restart.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_without_role_preserves_existing_role(tmp_path):
    """In-memory reconnect with role=None must not touch peer.role."""
    registry = _make_registry(tmp_path)
    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/admin",
        role=PeerRole.SERVICE,
    )

    await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/admin",
        role=None,
        peer_id=peer_id,
    )
    assert registry._peers[peer_id].role == PeerRole.SERVICE


@pytest.mark.asyncio
async def test_fresh_path_without_role_preserves_mapping_role(tmp_path):
    """Daemon-restart lifecycle for an old hook: _peers cleared, caller
    reconnects with role=None (no role in connect payload). The new live
    peer must inherit the mapping's stored role, not the AGENT default."""
    registry = _make_registry(tmp_path)

    first_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/admin",
        role=PeerRole.SERVICE,
    )
    assert registry._mappings[first_id].role == PeerRole.SERVICE

    registry._peers.clear()

    second_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/admin",
        role=None,
    )
    assert registry._peers[second_id].role == PeerRole.SERVICE
    assert registry._mappings[second_id].role == PeerRole.SERVICE


@pytest.mark.asyncio
async def test_find_or_allocate_mapping_preserves_role_when_none(tmp_path):
    """Unit-level guard: _find_or_allocate_mapping with role=None on reuse
    must leave mapping.role untouched."""
    registry = _make_registry(tmp_path)

    first = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/svc",
        role=PeerRole.ORCHESTRATOR,
    )
    display_name = registry._peers[first[0]].display_name

    sid = registry._find_or_allocate_mapping(
        display_name=display_name,
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/svc",
        role=None,
    )
    assert sid == first[0]
    assert registry._mappings[sid].role == PeerRole.ORCHESTRATOR


@pytest.mark.asyncio
async def test_update_peer_role_syncs_peer_and_mapping(tmp_path):
    """PATCH /peers/{id}/role backing method: rewrite role on both the live
    Peer and its persistent mapping so the new role survives the next
    daemon restart."""
    registry = _make_registry(tmp_path)

    peer_id, name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/director",
        role=PeerRole.AGENT,
    )

    updated = await registry.update_peer_role(name, PeerRole.ORCHESTRATOR)
    assert updated is True
    assert registry._peers[peer_id].role == PeerRole.ORCHESTRATOR
    assert registry._mappings[peer_id].role == PeerRole.ORCHESTRATOR


@pytest.mark.asyncio
async def test_update_peer_role_returns_false_for_unknown_peer(tmp_path):
    registry = _make_registry(tmp_path)
    assert await registry.update_peer_role("ghost", PeerRole.ORCHESTRATOR) is False


# ---------------------------------------------------------------------------
# beads-els.2 — reconnect peer_id reuse (identity-based, no explicit peer_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_restart_reuses_peer_id(tmp_path):
    """After daemon restart (_peers cleared, mappings survive on disk),
    reconnecting peer must get the SAME peer_id — not a fresh UUID."""
    registry = _make_registry(tmp_path)

    first_id, first_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/devops-worker",
        role=PeerRole.AGENT,
    )

    # Simulate daemon restart: in-memory peers gone, mappings loaded from disk.
    registry._peers.clear()

    second_id, second_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/devops-worker",
        role=PeerRole.AGENT,
    )

    assert second_id == first_id, (
        f"Expected peer_id reuse after daemon restart, got {second_id} != {first_id}"
    )
    assert second_name == first_name
    assert "-2-" not in second_name


@pytest.mark.asyncio
async def test_offline_peer_reuse_within_ttl(tmp_path):
    """OFFLINE peer in _peers with recent disconnect must be reused (sub-case A)."""
    registry = _make_registry(tmp_path)

    first_id, first_name = await registry.allocate_and_register(
        circle="dev",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/backend-head",
        role=PeerRole.ORCHESTRATOR,
    )

    # Peer goes offline (WS drop)
    await registry.mark_offline(first_id)
    assert registry._peers[first_id].status.value == "offline"

    # Reconnect without explicit peer_id — should reuse
    second_id, second_name = await registry.allocate_and_register(
        circle="dev",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/backend-head",
        role=PeerRole.ORCHESTRATOR,
    )

    assert second_id == first_id
    assert second_name == first_name
    assert registry._peers[second_id].status.value == "online"


@pytest.mark.asyncio
async def test_offline_peer_expired_ttl_gets_fresh_id(tmp_path):
    """OFFLINE peer whose last_seen exceeds TTL must NOT be reused for
    non-singleton roles — treat as genuine agent death."""
    import datetime as _dt

    registry = _make_registry(tmp_path)

    first_id, _ = await registry.allocate_and_register(
        circle="dev",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/some-worker",
        role=PeerRole.AGENT,
    )
    await registry.mark_offline(first_id)

    # Back-date last_seen well past TTL (default 120s)
    old_peer = registry._peers[first_id]
    old_peer.last_seen = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=300)

    second_id, second_name = await registry.allocate_and_register(
        circle="dev",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/some-worker",
        role=PeerRole.AGENT,
    )

    assert second_id != first_id, "Stale peer must not be reused after TTL expiry"


@pytest.mark.asyncio
async def test_singleton_role_reuses_without_ttl(tmp_path):
    """Singleton roles must be reused regardless of last_seen age."""
    import datetime as _dt

    cfg = Config()
    cfg.daemon.spawn.singleton_roles = ["pm"]

    path = tmp_path / "sessions.json"
    registry = PeerRegistry(
        config=cfg,
        message_router=MagicMock(),
        persistence_path=path,
    )

    first_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/pm",
        role=PeerRole.ORCHESTRATOR,
    )
    await registry.mark_offline(first_id)

    # Back-date far beyond any TTL
    old_peer = registry._peers[first_id]
    old_peer.last_seen = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)

    second_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/pm",
        role=PeerRole.ORCHESTRATOR,
    )

    assert second_id == first_id, "Singleton role must reuse peer_id regardless of age"


@pytest.mark.asyncio
async def test_daemon_restart_preserves_display_name_no_suffix(tmp_path):
    """End-to-end: no '-2' suffix after daemon restart reconnect."""
    registry = _make_registry(tmp_path)

    _, first_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/devops-worker",
    )
    assert "-2-" not in first_name

    registry._peers.clear()

    _, second_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/devops-worker",
    )

    assert second_name == first_name, (
        f"Display name changed after restart: {first_name!r} -> {second_name!r}"
    )
    assert "-2-" not in second_name
