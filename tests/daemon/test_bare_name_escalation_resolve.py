"""beads-7ijt.1: reliable bare-name resolution of escalation targets.

A secretary (a ``bypasses_circles`` peer — director/orchestrator, brain-admin/
service) escalates via the DOCUMENTED special-peer form ``notify_peer('telegram')``
/ ``notify_peer('director')`` (MCP notify_peer docstring; global CLAUDE.md). But
live peers register through ``allocate_and_register -> _build_display_name`` which
deterministically assigns ``<folder>-<backend>`` display_names
(``telegram-claude-code``, ``director-claude-code``). All public resolution matches
the target by EXACT ``display_name ==`` only — no bare-name/role-stem alias — so a
bare escalation matches ZERO peers and the route returns 404 "Unknown peer" (the
leak-incident: 1/4 escalation notify silently lost). Root cause reproduced live,
read-only: ``GET /peers/telegram`` -> 404, ``GET /peers/telegram-claude-code`` ->
200.

Fix (Variant A, director-approved): when the exact ``==`` match yields nothing,
fall back to a stem-alias resolve — strip the trailing ``-<backend>`` and match,
but ONLY against ``bypasses_circles`` peers (SERVICE/ORCHESTRATOR/HUMAN — exactly
the escalation targets), keeping the blast radius minimal. The fallback is
ambiguity-aware (reuses the bof3 AmbiguousPeerError path) so a stem matching >1
circle for a cross-capable caller still fails fast instead of a silent pick.

NOTE on bof3 (beads-bof3): bof3's AmbiguousPeerError is a DISTINCT invariant — it
fires on the FULL display_name ``pm-claude-code`` present in >1 circle, NOT on a
bare ``pm``. Variant A does NOT alias regular AGENT namesakes, so bof3 is untouched.
The two invariants are asserted separately below.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from repowire.config.models import Config
from repowire.daemon.message_router import MessageRouter
from repowire.daemon.peer_registry import AmbiguousPeerError, PeerRegistry
from repowire.protocol.peers import Peer, PeerRole, PeerStatus


@pytest.fixture
def mock_message_router():
    router = MagicMock(spec=MessageRouter)
    router.send_query = AsyncMock(return_value="mock response")
    router.send_notification = AsyncMock()
    router.broadcast = AsyncMock(return_value=[])
    return router


async def _register(pm, sid, name, circle, role=PeerRole.AGENT, status=PeerStatus.ONLINE):
    peer = Peer(
        peer_id=sid, display_name=name, path=f"/{name}",
        machine="localhost", circle=circle, role=role, status=status,
    )
    await pm.register_peer(peer)
    return peer


# ---------------------------------------------------------------------------
# Core: bare special-peer names resolve to the backend-suffixed display_name
# (the escalation path). Targets register with the FULL suffixed display_name,
# exactly as the live daemon mints them via _build_display_name.
# ---------------------------------------------------------------------------


class TestBareNameEscalationResolve:
    async def test_bare_telegram_resolves_suffixed_service_peer(self, mock_message_router):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-tg", "telegram-claude-code", "global", role=PeerRole.SERVICE)
        peer = pm._resolve_unique_unlocked("telegram", raise_ambiguous=True)
        assert peer is not None
        assert peer.peer_id == "sid-tg"

    async def test_bare_director_resolves_suffixed_orchestrator_peer(
        self, mock_message_router
    ):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(
            pm, "sid-dir", "director-claude-code", "global", role=PeerRole.ORCHESTRATOR
        )
        peer = pm._resolve_unique_unlocked("director", raise_ambiguous=True)
        assert peer is not None
        assert peer.peer_id == "sid-dir"

    async def test_exact_suffixed_name_still_resolves(self, mock_message_router):
        # The fallback must not disturb the existing exact-match path.
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-tg", "telegram-claude-code", "global", role=PeerRole.SERVICE)
        peer = pm._resolve_unique_unlocked("telegram-claude-code", raise_ambiguous=True)
        assert peer.peer_id == "sid-tg"

    async def test_secretary_notify_bare_director_delivers(self, mock_message_router):
        """End-to-end escalation: a bypasses_circles secretary (brain-admin,
        SERVICE) notifies bare 'director' WITHOUT circle= -> delivered."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(
            pm, "sid-ba", "brain-admin-claude-code", "global", role=PeerRole.SERVICE
        )
        await _register(
            pm, "sid-dir", "director-claude-code", "global", role=PeerRole.ORCHESTRATOR
        )
        await pm.notify(
            from_peer="brain-admin-claude-code",
            from_peer_id="sid-ba",
            to_peer="director",
            text="escalation",
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-dir"

    async def test_secretary_query_bare_telegram_delivers(self, mock_message_router):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(
            pm, "sid-dir", "director-claude-code", "global", role=PeerRole.ORCHESTRATOR
        )
        await _register(
            pm, "sid-tg", "telegram-claude-code", "global", role=PeerRole.SERVICE
        )
        await pm.query(
            from_peer="director-claude-code",
            from_peer_id="sid-dir",
            to_peer="telegram",
            text="status?",
        )
        _, kwargs = mock_message_router.send_query.call_args
        assert kwargs["to_session_id"] == "sid-tg"


# ---------------------------------------------------------------------------
# Variant A narrowness + bof3 preservation. Two DISTINCT invariants (director
# correction): bof3 fires on the FULL 'pm-claude-code' in >1 circle; a bare 'pm'
# is simply not aliased (regular AGENT, not bypasses_circles).
# ---------------------------------------------------------------------------


class TestVariantANarrowness:
    async def test_bare_agent_stem_not_aliased(self, mock_message_router):
        """A regular AGENT namesake is NOT reachable by its bare role-stem —
        Variant A only aliases bypasses_circles targets."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(
            pm, "sid-bh", "backend-head-claude-code", "project-x", role=PeerRole.AGENT
        )
        assert pm._resolve_unique_unlocked("backend-head", raise_ambiguous=True) is None

    async def test_bof3_full_name_ambiguity_still_raises(self, mock_message_router):
        """DoD-2 (corrected): bof3 invariant triggers on the FULL display_name
        'pm-claude-code' present in >1 circle for a cross-capable caller."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-drafter", "pm-claude-code", "project-drafter")
        await _register(pm, "sid-abt", "pm-claude-code", "project-agents-brain-team")
        with pytest.raises(AmbiguousPeerError) as exc:
            pm._resolve_unique_unlocked("pm-claude-code", raise_ambiguous=True)
        assert exc.value.circles == [
            "project-agents-brain-team",
            "project-drafter",
        ]

    async def test_bare_pm_not_aliased_separate_invariant(self, mock_message_router):
        """Separate invariant proving A's narrowness: a bare 'pm' does NOT resolve
        and does NOT raise — regular AGENT namesakes are never stem-aliased, so the
        bof3 (full-name) ambiguity is the only pm ambiguity path."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-drafter", "pm-claude-code", "project-drafter")
        await _register(pm, "sid-abt", "pm-claude-code", "project-agents-brain-team")
        assert pm._resolve_unique_unlocked("pm", raise_ambiguous=True) is None

    async def test_bare_alias_ambiguous_across_circles_raises(self, mock_message_router):
        """The stem-alias fallback stays bof3-faithful: if two bypasses_circles
        peers share a stem across DIFFERENT circles, a cross-capable bare address
        fails fast (AmbiguousPeerError) instead of a silent preference pick."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-a", "svc-claude-code", "global", role=PeerRole.SERVICE)
        await _register(pm, "sid-b", "svc-claude-code", "circle-b", role=PeerRole.SERVICE)
        with pytest.raises(AmbiguousPeerError) as exc:
            pm._resolve_unique_unlocked("svc", raise_ambiguous=True)
        assert exc.value.circles == ["circle-b", "global"]

    async def test_bare_alias_ambiguous_but_raise_disabled_best_effort(
        self, mock_message_router
    ):
        """With raise_ambiguous=False (internal callers) the stem-alias must not
        raise — best-effort pick, matching the exact-match path's contract."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-a", "svc-claude-code", "global", role=PeerRole.SERVICE)
        await _register(pm, "sid-b", "svc-claude-code", "circle-b", role=PeerRole.SERVICE)
        peer = pm._resolve_unique_unlocked("svc", raise_ambiguous=False)
        assert peer is not None  # best-effort, no raise
