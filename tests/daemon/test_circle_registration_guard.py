"""Daemon-side guard against registering peers into a grouped-session view circle.

Defense-in-depth for the q2ok circle-misregistration outage (beads-lyfk,
A-task-2). Even if the ws-hook fallback (A-task-1) is bypassed — an old hook, a
non-standard spawn path, or a client that sends a raw ``#{session_name}`` — the
daemon must never store a peer in a ``<base>-view-<suffix>`` circle. The fix
normalizes the circle inside ``allocate_and_register``, the single chokepoint
through which every registration path flows.

These are pure registry unit tests: ``PeerRegistry`` is constructed directly
with an isolated tmpdir persistence path and a mocked message router. No live
daemon, socket, or tmux is touched (q2ok RELEASE-GATE).
"""

from __future__ import annotations

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


@pytest.mark.asyncio
async def test_allocate_normalizes_view_circle_to_base(tmp_path):
    """A connect carrying circle=``global-view-agents-brain-team`` (the live
    q2ok director symptom) must register the peer in ``global``, not the view."""
    registry = _make_registry(tmp_path)

    peer_id, _ = await registry.allocate_and_register(
        circle="global-view-agents-brain-team",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/director",
        role=PeerRole.ORCHESTRATOR,
    )

    assert registry._peers[peer_id].circle == "global"
    # session_id encodes the circle as ``repow-{circle}-{uuid8}``; the view
    # alias must not leak into the peer_id either.
    assert "-view-" not in peer_id


@pytest.mark.asyncio
async def test_allocate_preserves_plain_circle(tmp_path):
    """A legitimate project circle (no view marker) is registered unchanged —
    the guard must not mangle real circles (Tilix two-pane UI stays intact)."""
    registry = _make_registry(tmp_path)

    peer_id, _ = await registry.allocate_and_register(
        circle="project-drafter",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/drafter-pm",
        role=PeerRole.ORCHESTRATOR,
    )

    assert registry._peers[peer_id].circle == "project-drafter"
