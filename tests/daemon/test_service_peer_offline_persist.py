"""Regression tests for beads-7ijt.

User-facing service peers (SERVICE / ORCHESTRATOR / HUMAN — i.e.
``bypasses_circles``) must persist in the registry even while OFFLINE, so
the user can still see and message them in Telegram and the agent-gateway
respawns them on demand.

Root cause: two deletion paths removed OFFLINE peers without any role
exemption —

* ``_evict_stale_peers`` — age-based prune (``prune_max_age_hours``, 24h
  default). The confirmed mechanism: brain-admin (``auto_respawn=False``)
  sat OFFLINE past 24h and was evicted, while director
  (``auto_respawn=True`` → almost always ONLINE) never aged into it.
* ``_purge_stale_role_siblings_unlocked`` — spawn-time purge of OFFLINE
  role-siblings. Does not fire for today's non-timestamped service
  display_names, but exempted here as defense-in-depth so a future
  timestamped form can never evict a user-facing service peer.

Regular AGENT peers are still evicted/purged — only ``bypasses_circles``
roles are exempt.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repowire.config.models import AgentType, Config
from repowire.daemon.peer_registry import PeerRegistry
from repowire.protocol.peers import PeerRole


def _make_registry(tmp_path: Path) -> PeerRegistry:
    return PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        persistence_path=tmp_path / "sessions.json",
    )


def _backdate(registry: PeerRegistry, peer_id: str, seconds: float) -> None:
    registry._peers[peer_id].last_seen = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)
    )


# ---------------------------------------------------------------------------
# _evict_stale_peers — age-based prune (the confirmed root-cause path)
# ---------------------------------------------------------------------------

_BYPASS_ROLES = [PeerRole.SERVICE, PeerRole.ORCHESTRATOR, PeerRole.HUMAN]


@pytest.mark.parametrize("role", _BYPASS_ROLES)
@pytest.mark.asyncio
async def test_evict_preserves_offline_bypass_peer(tmp_path, role):
    """A long-OFFLINE bypasses_circles peer must NOT be evicted by age."""
    registry = _make_registry(tmp_path)
    pid, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/ai-infra/agents/brain-admin",
        role=role,
    )
    await registry.mark_offline(pid)
    # Older than prune_max_age_hours (24h default).
    _backdate(registry, pid, 25 * 3600)

    evicted = await registry._evict_stale_peers()

    assert evicted == 0
    assert pid in registry._peers, f"{role} peer must persist offline"


@pytest.mark.asyncio
async def test_evict_still_removes_offline_agent_peer(tmp_path):
    """Contrast: a plain AGENT peer past max-age is still evicted."""
    registry = _make_registry(tmp_path)
    pid, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-1000",
        role=PeerRole.AGENT,
    )
    await registry.mark_offline(pid)
    _backdate(registry, pid, 25 * 3600)

    evicted = await registry._evict_stale_peers()

    assert evicted == 1
    assert pid not in registry._peers


@pytest.mark.asyncio
async def test_evict_mixed_keeps_service_drops_agent(tmp_path):
    """A single sweep keeps the service peer and drops the stale agent."""
    registry = _make_registry(tmp_path)
    svc_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/ai-infra/agents/brain-admin",
        role=PeerRole.SERVICE,
    )
    agent_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-1000",
        role=PeerRole.AGENT,
    )
    for pid in (svc_id, agent_id):
        await registry.mark_offline(pid)
        _backdate(registry, pid, 25 * 3600)

    evicted = await registry._evict_stale_peers()

    assert evicted == 1
    assert svc_id in registry._peers
    assert agent_id not in registry._peers


# ---------------------------------------------------------------------------
# _purge_stale_role_siblings_unlocked — spawn-time purge (defense-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_sibling_purge_preserves_offline_service(tmp_path):
    """A timestamped OFFLINE service sibling must survive a fresh spawn that
    shares its role-stem (AGENT siblings would be purged here)."""
    registry = _make_registry(tmp_path)
    old_id, old_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/brain-admin-1000",
        role=PeerRole.SERVICE,
    )
    assert old_name == "brain-admin-1000-claude-code"
    await registry.mark_offline(old_id)
    _backdate(registry, old_id, 600)  # past the 300s purge threshold

    new_id, new_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/brain-admin-2000",
        role=PeerRole.SERVICE,
    )
    assert new_name == "brain-admin-2000-claude-code"
    assert new_id != old_id
    assert old_id in registry._peers, "offline service sibling must not be purged"
