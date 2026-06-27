"""beads-bof3: fail-fast on cross-circle peer-addressing ambiguity.

The mesh allows the same display_name in different circles (e.g. a pm in
project-drafter AND project-agents-brain-team). A public notify/ask/kill that
names such a peer WITHOUT a circle used to resolve SILENTLY to one of them via a
preference tiebreak (peer_registry._lookup_peer_unlocked) → mis-delivery
(observed: a director release-ACK for the agents-brain-team pm landed on the
drafter pm).

The fix is the HYBRID design (director GO(A)):
- Layer 1 (UNTOUCHED): a project-scoped authenticated sender auto-disambiguates
  to a namesake in its OWN circle (beads-hqvm leak fix). In its own circle the
  namesake is unique — that is correctness, not a guess.
- Layer 2 (THIS change): when resolution AFTER Layer 1 still matches >1 circle and
  no circle/to_peer_id was given, a cross-circle-capable sender (bypass /
  orchestrator / unresolved) raises AmbiguousPeerError on the PUBLIC path instead
  of silently picking. The internal best-effort tiebreak survives for liveness /
  repair (which must never raise).
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


def _bias_preference_to(pm, *winning_sids):
    """Make the given session_ids win the preference tiebreak (mock 'connected')."""
    transport = MagicMock()
    transport.is_connected = lambda sid: sid in winning_sids
    pm._transport = transport


# ---------------------------------------------------------------------------
# Task 1: AmbiguousPeerError carries an actionable, sorted circle list
# ---------------------------------------------------------------------------


class TestAmbiguousPeerError:
    def test_message_lists_circles_and_says_specify_circle(self):
        # The resolver passes circles already sorted; the error joins verbatim.
        err = AmbiguousPeerError(
            "pm-claude-code", ["project-agents-brain-team", "project-drafter"]
        )
        assert str(err) == (
            "ambiguous peer 'pm-claude-code': matches "
            "[project-agents-brain-team, project-drafter], specify circle="
        )

    def test_is_value_error_subclass(self):
        # Subclass of ValueError so existing route ValueError->error handling
        # catches it without changes.
        assert issubclass(AmbiguousPeerError, ValueError)

    def test_exposes_name_and_circles_attributes(self):
        err = AmbiguousPeerError("pm-claude-code", ["a", "b"])
        assert err.name == "pm-claude-code"
        assert err.circles == ["a", "b"]


# ---------------------------------------------------------------------------
# Task 2: _resolve_unique_unlocked strict resolver
# ---------------------------------------------------------------------------


class TestResolveUniqueUnlocked:
    async def test_zero_matches_returns_none(self, mock_message_router):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        assert pm._resolve_unique_unlocked("nobody", raise_ambiguous=True) is None

    async def test_single_match_returns_peer(self, mock_message_router):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-a", "solo", "circle-a")
        peer = pm._resolve_unique_unlocked("solo", raise_ambiguous=True)
        assert peer is not None
        assert peer.peer_id == "sid-a"

    async def test_exact_peer_id_match_wins(self, mock_message_router):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-a", "pm-claude-code", "circle-a")
        await _register(pm, "sid-b", "pm-claude-code", "circle-b")
        peer = pm._resolve_unique_unlocked("sid-b", raise_ambiguous=True)
        assert peer.peer_id == "sid-b"

    async def test_cross_circle_ambiguous_raises_with_sorted_circles(
        self, mock_message_router
    ):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-drafter", "pm-claude-code", "project-drafter")
        await _register(pm, "sid-abt", "pm-claude-code", "project-agents-brain-team")
        with pytest.raises(AmbiguousPeerError) as exc:
            pm._resolve_unique_unlocked("pm-claude-code", raise_ambiguous=True)
        assert exc.value.circles == [
            "project-agents-brain-team",
            "project-drafter",
        ]

    async def test_ambiguous_but_raise_disabled_falls_back_to_best_effort(
        self, mock_message_router
    ):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-drafter", "pm-claude-code", "project-drafter")
        await _register(pm, "sid-abt", "pm-claude-code", "project-agents-brain-team")
        _bias_preference_to(pm, "sid-abt")
        peer = pm._resolve_unique_unlocked("pm-claude-code", raise_ambiguous=False)
        assert peer.peer_id == "sid-abt"  # best-effort preference tiebreak, no raise

    async def test_explicit_circle_scopes_and_does_not_raise(self, mock_message_router):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-drafter", "pm-claude-code", "project-drafter")
        await _register(pm, "sid-abt", "pm-claude-code", "project-agents-brain-team")
        peer = pm._resolve_unique_unlocked(
            "pm-claude-code", circle="project-drafter", raise_ambiguous=True
        )
        assert peer.peer_id == "sid-drafter"

    async def test_same_circle_duplicate_does_not_raise(self, mock_message_router):
        # Registry anomaly: same display_name twice in ONE circle. NOT a
        # cross-circle ambiguity (>1 DIFFERENT circle), so best-effort stays.
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-1", "dup", "circle-a")
        await _register(pm, "sid-2", "dup", "circle-a")
        _bias_preference_to(pm, "sid-2")
        peer = pm._resolve_unique_unlocked("dup", raise_ambiguous=True)
        assert peer.peer_id == "sid-2"  # one circle only → best-effort, no raise


# ---------------------------------------------------------------------------
# Task 4: regression on the EXACT incident — director(bypasses_circles) → pm
# without a circle must fail-fast, not silently preference-pick (peer_registry
# L294-302). Plus Layer-1 / bypass-target invariants stay intact.
# ---------------------------------------------------------------------------


class TestCrossCircleIncidentRegression:
    @staticmethod
    async def _two_pms_and_director(pm):
        # Two pm namesakes in different circles + a director that bypasses circles.
        await _register(pm, "sid-drafter-pm", "pm-claude-code", "project-drafter")
        await _register(
            pm, "sid-abt-pm", "pm-claude-code", "project-agents-brain-team"
        )
        await _register(
            pm, "sid-dir", "director-claude-code", "global",
            role=PeerRole.ORCHESTRATOR,
        )

    async def test_director_notify_ambiguous_pm_raises(self, mock_message_router):
        """THE incident: director release-ACK for the agents-brain-team pm must
        NOT silently land on the drafter pm — raise listing BOTH circles."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._two_pms_and_director(pm)
        # Bias the wrong (drafter) namesake to win a blind preference pick.
        _bias_preference_to(pm, "sid-drafter-pm")

        with pytest.raises(AmbiguousPeerError) as exc:
            await pm.notify(
                from_peer="director-claude-code",
                from_peer_id="sid-dir",
                to_peer="pm-claude-code",
                text="release ACK",
            )
        assert exc.value.circles == [
            "project-agents-brain-team",
            "project-drafter",
        ]
        mock_message_router.send_notification.assert_not_called()

    async def test_director_query_ambiguous_pm_raises(self, mock_message_router):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._two_pms_and_director(pm)
        with pytest.raises(AmbiguousPeerError):
            await pm.query(
                from_peer="director-claude-code",
                from_peer_id="sid-dir",
                to_peer="pm-claude-code",
                text="status?",
            )
        mock_message_router.send_query.assert_not_called()

    async def test_director_notify_with_circle_resolves_correct_pm(
        self, mock_message_router
    ):
        """Explicit circle= disambiguates → delivers to the RIGHT pm, no raise."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._two_pms_and_director(pm)
        _bias_preference_to(pm, "sid-drafter-pm")  # wrong one would win blind

        await pm.notify(
            from_peer="director-claude-code",
            from_peer_id="sid-dir",
            to_peer="pm-claude-code",
            circle="project-agents-brain-team",
            text="release ACK",
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-abt-pm"

    async def test_bypass_target_unique_global_still_delivers(
        self, mock_message_router
    ):
        """ACCENT 3: a bypasses_circles target (director/telegram/brain-admin) is
        globally unique → no ambiguity, delivery works (not over-blocked)."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-w", "backend-worker", "project-zeon")
        await _register(
            pm, "sid-dir", "director-claude-code", "global",
            role=PeerRole.ORCHESTRATOR,
        )
        await pm.notify(
            from_peer="backend-worker",
            from_peer_id="sid-w",
            to_peer="director-claude-code",
            text="report",
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-dir"

    async def test_layer1_project_sender_resolves_own_circle_no_raise(
        self, mock_message_router
    ):
        """Layer-1 INTACT: a project-scoped authenticated sender notifying an
        ambiguous namesake WITHOUT a circle auto-disambiguates to the namesake in
        its OWN circle (beads-hqvm), not a fail-fast."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-drafter-pm", "pm-claude-code", "project-drafter")
        await _register(pm, "sid-zeon-pm", "pm-claude-code", "project-zeon")
        await _register(pm, "sid-zeon-head", "backend-head", "project-zeon")
        _bias_preference_to(pm, "sid-drafter-pm")  # foreign one would win blind

        await pm.notify(
            from_peer="backend-head",
            from_peer_id="sid-zeon-head",
            to_peer="pm-claude-code",
            text="status",
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-zeon-pm"  # own circle, no raise

    async def test_internal_lookup_does_not_raise_on_ambiguity(
        self, mock_message_router
    ):
        """Internal liveness/repair via _lookup_peer_unlocked keeps best-effort —
        it must NEVER raise on an ambiguous name (would break repair paths)."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await _register(pm, "sid-drafter-pm", "pm-claude-code", "project-drafter")
        await _register(
            pm, "sid-abt-pm", "pm-claude-code", "project-agents-brain-team"
        )
        _bias_preference_to(pm, "sid-abt-pm")
        peer = pm._lookup_peer_unlocked("pm-claude-code")  # no raise
        assert peer.peer_id == "sid-abt-pm"
