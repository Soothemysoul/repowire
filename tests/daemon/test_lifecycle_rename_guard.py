"""Guard: an after-rename-session event for a grouped-session *view* alias must
not clobber peer circles.

Tertiary defense for the q2ok outage (beads-lyfk, A-task-3). The original RCA
hypothesis was that the global after-rename-session hook fired with
``new_name=global-view-agents-brain-team`` and ``handle_session_renamed``
blindly moved every pane's peer onto that view alias — yanking director /
brain-admin / librarian out of circle ``global``. Even though the live incident
turned out to be a registration-time path (A-task-1/2), this rename path must be
hardened so it can never re-introduce the clobber if it ever fires.

Isolated unit test: ``LifecycleHandler`` over a direct ``PeerRegistry`` (tmpdir
persistence, mocked tracker/transport). No live daemon, socket, or tmux.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repowire.config.models import AgentType, Config
from repowire.daemon.lifecycle_handler import LifecycleHandler
from repowire.daemon.peer_registry import PeerRegistry
from repowire.protocol.peers import PeerRole


def _make_handler(tmp_path: Path) -> tuple[LifecycleHandler, PeerRegistry]:
    registry = PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        persistence_path=tmp_path / "sessions.json",
    )
    handler = LifecycleHandler(
        peer_registry=registry,
        query_tracker=MagicMock(),
        transport=MagicMock(),
    )
    return handler, registry


@pytest.mark.asyncio
async def test_view_session_rename_does_not_clobber_circle(tmp_path):
    """A rename whose new_name is a view alias (<base>-view-<suffix>) must leave
    the peer's circle untouched — this is the director clobber path."""
    handler, registry = _make_handler(tmp_path)
    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/director",
        pane_id="%64",
        role=PeerRole.ORCHESTRATOR,
    )

    moved = await handler.handle_session_renamed(
        new_name="global-view-agents-brain-team", pane_ids=["%64"],
    )

    assert moved == 0
    assert registry._peers[peer_id].circle == "global"


@pytest.mark.asyncio
async def test_legitimate_base_rename_still_moves_circle(tmp_path):
    """Control: a real base-session rename (no view marker) must still move the
    peer's circle, so the guard does not break legitimate renames."""
    handler, registry = _make_handler(tmp_path)
    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/director",
        pane_id="%64",
        role=PeerRole.ORCHESTRATOR,
    )

    moved = await handler.handle_session_renamed(
        new_name="global-renamed", pane_ids=["%64"],
    )

    assert moved == 1
    assert registry._peers[peer_id].circle == "global-renamed"
