"""Tests for circles (logical subnet) feature.

Covers: data models (Peer, PeerConfig), and access control via the public query() API.
Circle enforcement now uses the live peer registry (not config.yaml).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from repowire.config.models import Config, PeerConfig
from repowire.daemon.message_router import MessageRouter
from repowire.daemon.peer_registry import PeerRegistry
from repowire.protocol.peers import Peer, PeerRole, PeerStatus

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_message_router():
    """Mock MessageRouter – send_query returns a canned response."""
    router = MagicMock(spec=MessageRouter)
    router.send_query = AsyncMock(return_value="mock response")
    router.send_notification = AsyncMock()
    router.broadcast = AsyncMock(return_value=[])
    return router


@pytest.fixture
def make_peer_manager(mock_message_router):
    """Factory fixture: create a PeerRegistry with the given Config."""

    def _make(config: Config | None = None) -> PeerRegistry:
        return PeerRegistry(
            config=config or Config(),
            message_router=mock_message_router,
        )

    return _make


# ---------------------------------------------------------------------------
# Peer model – circle field
# ---------------------------------------------------------------------------


class TestPeerCircleField:
    """Tests for circle field in Peer model."""

    def test_peer_default_circle_is_global(self):
        """Peer model should have 'global' as default circle."""
        peer = Peer(name="test", path="/test", machine="localhost")
        assert peer.circle == "global"

    def test_peer_circle_in_to_dict(self):
        """Peer.to_dict() should include circle."""
        peer = Peer(name="test", path="/test", machine="localhost", circle="my-circle")
        data = peer.to_dict()
        assert data["circle"] == "my-circle"

    def test_peer_circle_from_constructor(self):
        """Peer constructor should preserve circle."""
        peer = Peer(
            name="test",
            path="/test",
            machine="localhost",
            circle="my-circle",
            status=PeerStatus.ONLINE,
        )
        assert peer.circle == "my-circle"


# ---------------------------------------------------------------------------
# PeerConfig – circle field
# ---------------------------------------------------------------------------


class TestPeerConfigCircle:
    """Tests for circle field in PeerConfig."""

    def test_peer_config_circle_field(self):
        """PeerConfig should have optional circle field."""
        peer_config = PeerConfig(name="test", circle="my-circle")
        assert peer_config.circle == "my-circle"

    def test_peer_config_circle_default_none(self):
        """PeerConfig circle should default to None."""
        peer_config = PeerConfig(name="test")
        assert peer_config.circle is None


# ---------------------------------------------------------------------------
# Circle access control (tested through public query() API)
# Now enforced from live peer registry, not config.yaml
# ---------------------------------------------------------------------------


class TestCircleAccessControl:
    """Tests for circle-based access control via query()."""

    @staticmethod
    async def _register(pm: PeerRegistry, name: str, circle: str) -> None:
        """Register a peer with the given name and circle."""
        peer = Peer(
            peer_id=f"repow-{circle}-{name}",
            display_name=name,
            path=f"/{name}",
            machine="localhost",
            circle=circle,
        )
        await pm.register_peer(peer)

    async def test_same_circle_query_succeeds(self, mock_message_router):
        """Peers in the same circle can query each other."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "peer-a", "dev")
        await self._register(pm, "peer-b", "dev")

        result = await pm.query("peer-a", "peer-b", "hello")
        assert result == "mock response"

    async def test_cross_circle_query_blocked(self, mock_message_router):
        """Peers in different circles cannot query each other."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "peer-a", "dev")
        await self._register(pm, "peer-b", "staging")

        with pytest.raises(ValueError, match="Circle boundary"):
            await pm.query("peer-a", "peer-b", "hello")

    async def test_bypass_circle_query_succeeds(self, mock_message_router):
        """bypass_circle=True allows cross-circle queries."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "peer-a", "dev")
        await self._register(pm, "peer-b", "staging")

        result = await pm.query("peer-a", "peer-b", "hello", bypass_circle=True)
        assert result == "mock response"

    async def test_unknown_sender_non_bypass_blocked(self, mock_message_router):
        """Unknown (unresolved) sender without bypass is BLOCKED (beads-hqvm DoD7).

        Previously an unresolved from_obj got a free pass through the circle
        guard (early-return-on-None). That hole is now closed: a non-bypass
        caller whose identity cannot be resolved is denied. Real CLI callers
        reach the daemon with bypass=True (routes/messages.py auto-bypass), so
        they are unaffected — see test_unknown_sender_with_bypass_allowed.
        """
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "peer-b", "staging")

        with pytest.raises(ValueError, match="Circle boundary"):
            await pm.query("cli", "peer-b", "hello")

    async def test_unknown_sender_with_bypass_allowed(self, mock_message_router):
        """CLI callers (bypass=True) are unaffected by the guard hardening."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "peer-b", "staging")

        result = await pm.query("cli", "peer-b", "hello", bypass_circle=True)
        assert result == "mock response"


# ---------------------------------------------------------------------------
# Same-name peers in different circles
# ---------------------------------------------------------------------------


class TestSameNameDifferentCircles:
    """Tests that query/notify target the correct peer when two peers share a display_name."""

    @staticmethod
    async def _register(pm: PeerRegistry, session_id: str, name: str, circle: str) -> None:
        peer = Peer(
            peer_id=session_id,
            display_name=name,
            path=f"/{name}",
            machine="localhost",
            circle=circle,
        )
        await pm.register_peer(peer)

    async def test_query_targets_correct_circle(self, mock_message_router):
        """query(..., circle='teamA') routes to the teamA peer, not teamB."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-a", "myproject", "teamA")
        await self._register(pm, "sid-b", "myproject", "teamB")

        # CLI-style caller (unresolved sender) reaches the daemon with bypass.
        await pm.query("cli", "myproject", "hello", circle="teamA", bypass_circle=True)

        mock_message_router.send_query.assert_called_once()
        _, kwargs = mock_message_router.send_query.call_args
        assert kwargs["to_session_id"] == "sid-a"

    async def test_query_wrong_circle_raises(self, mock_message_router):
        """query with circle that doesn't exist raises ValueError."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-a", "myproject", "teamA")

        with pytest.raises(ValueError, match="Unknown peer"):
            await pm.query("cli", "myproject", "hello", circle="teamZ")

    async def test_notify_targets_correct_circle(self, mock_message_router):
        """notify(..., circle='teamB') routes to the teamB peer."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-a", "myproject", "teamA")
        await self._register(pm, "sid-b", "myproject", "teamB")

        await pm.notify("cli", "myproject", "hi", circle="teamB", bypass_circle=True)

        mock_message_router.send_notification.assert_called_once()
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-b"

    async def test_notify_no_circle_picks_online_peer(
        self, mock_message_router):
        """notify with no circle falls back to online-first tiebreaking."""
        from repowire.protocol.peers import PeerStatus

        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-a", "myproject", "teamA")
        await self._register(pm, "sid-b", "myproject", "teamB")

        # Mark sid-a offline so sid-b wins the tiebreak
        async with pm._lock:
            pm._peers["sid-a"].status = PeerStatus.OFFLINE

        await pm.notify("cli", "myproject", "hi", bypass_circle=True)

        mock_message_router.send_notification.assert_called_once()
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-b"

    async def test_circle_access_checked_with_resolved_peers(
        self, mock_message_router):
        """Circle check uses resolved Peer objects, not ambiguous display_names."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        # sender is in teamA; two "myproject" targets in teamA and teamB
        await self._register(pm, "sid-sender", "sender", "teamA")
        await self._register(pm, "sid-a", "myproject", "teamA")
        await self._register(pm, "sid-b", "myproject", "teamB")

        # Query teamA target — sender and target are in same circle: should succeed
        await pm.query("sender", "myproject", "hello", circle="teamA")
        mock_message_router.send_query.assert_called_once()

    async def test_cross_circle_blocked_with_resolved_peers(
        self, mock_message_router):
        """When target circle differs from sender circle, access is blocked."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-sender", "sender", "teamA")
        await self._register(pm, "sid-b", "myproject", "teamB")

        with pytest.raises(ValueError, match="Circle boundary"):
            await pm.query("sender", "myproject", "hello", circle="teamB")


# ---------------------------------------------------------------------------
# from_peer circle-preferred lookup (Fix 3 regression)
# ---------------------------------------------------------------------------


class TestFromPeerCircleLookup:
    """Regression tests: from_peer is resolved preferring target's circle first."""

    @staticmethod
    async def _register(pm: PeerRegistry, session_id: str, name: str, circle: str) -> None:
        peer = Peer(
            peer_id=session_id,
            display_name=name,
            path=f"/{name}",
            machine="localhost",
            circle=circle,
        )
        await pm.register_peer(peer)

    async def test_same_name_sender_in_same_circle_no_false_boundary(
        self, mock_message_router):
        """sender and target share display_name pattern; sender in same circle — no error."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        # Two senders with same display_name in different circles
        await self._register(pm, "sid-sender-a", "orchestrator", "teamA")
        await self._register(pm, "sid-sender-b", "orchestrator", "teamB")
        await self._register(pm, "sid-target", "worker", "teamA")

        # from_peer="orchestrator" should resolve to teamA (target's circle), not teamB
        result = await pm.query("orchestrator", "worker", "hello")
        assert result == "mock response"

    async def test_sender_circle_mismatch_still_blocked(
        self, mock_message_router):
        """If the only matching sender is in a different circle, boundary is enforced."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-sender", "orchestrator", "teamB")
        await self._register(pm, "sid-target", "worker", "teamA")

        with pytest.raises(ValueError, match="Circle boundary"):
            await pm.query("orchestrator", "worker", "hello")


# ---------------------------------------------------------------------------
# Authenticated sender identity (from_peer_id) — circle-scoped resolution
# beads-hqvm: fix the cross-circle LEAK when BOTH to_peer and from_peer collide
# ---------------------------------------------------------------------------


class TestAuthenticatedSenderResolution:
    """from_peer_id (authenticated identity, pane->peer_id) scopes target
    resolution to the SENDER's circle.

    This closes the leak where, with both display_names colliding, the target
    preference-tiebreak picks the wrong-circle namesake and the sender then
    resolves (by name) to that same wrong circle, so the circle guard sees a
    spurious same-circle pair and silently delivers across circles.
    """

    @staticmethod
    async def _register(pm, sid, name, circle, role=PeerRole.AGENT):
        peer = Peer(
            peer_id=sid, display_name=name, path=f"/{name}",
            machine="localhost", circle=circle, role=role,
        )
        await pm.register_peer(peer)

    @staticmethod
    def _bias_preference_to(pm, *winning_sids):
        """Make the given session_ids win the preference tiebreak (mock 'connected')."""
        transport = MagicMock()
        transport.is_connected = lambda sid: sid in winning_sids
        pm._transport = transport

    async def test_double_collision_notify_routes_to_sender_circle(self, mock_message_router):
        """T1/T8: both names collide; from_peer_id -> target scoped to sender circle."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-abt-head", "backend-head", "circle-abt")
        await self._register(pm, "sid-abt-worker", "backend-worker", "circle-abt")
        await self._register(pm, "sid-zeon-head", "backend-head", "circle-zeon")
        await self._register(pm, "sid-zeon-worker", "backend-worker", "circle-zeon")
        # Without scoping the wrong-circle (abt) namesakes would win preference.
        self._bias_preference_to(pm, "sid-abt-head", "sid-abt-worker")

        await pm.notify(
            from_peer="backend-worker",
            from_peer_id="sid-zeon-worker",
            to_peer="backend-head",
            text="ACK",
        )

        mock_message_router.send_notification.assert_called_once()
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-zeon-head"

    async def test_double_collision_no_silent_foreign_delivery(self, mock_message_router):
        """T2: never silently deliver to the foreign-circle namesake."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-abt-head", "backend-head", "circle-abt")
        await self._register(pm, "sid-zeon-head", "backend-head", "circle-zeon")
        await self._register(pm, "sid-zeon-worker", "backend-worker", "circle-zeon")
        self._bias_preference_to(pm, "sid-abt-head")

        await pm.notify(
            from_peer="backend-worker",
            from_peer_id="sid-zeon-worker",
            to_peer="backend-head",
            text="ACK",
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] != "sid-abt-head"

    async def test_double_collision_query_routes_to_sender_circle(self, mock_message_router):
        """T1 (query/ask_peer path): scopes target to sender circle."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-abt-head", "backend-head", "circle-abt")
        await self._register(pm, "sid-zeon-head", "backend-head", "circle-zeon")
        await self._register(pm, "sid-zeon-worker", "backend-worker", "circle-zeon")
        self._bias_preference_to(pm, "sid-abt-head")

        await pm.query(
            "backend-worker", "backend-head", "hi", from_peer_id="sid-zeon-worker"
        )
        _, kwargs = mock_message_router.send_query.call_args
        assert kwargs["to_session_id"] == "sid-zeon-head"

    async def test_pm_pm_collision_both_circles(self, mock_message_router):
        """T8: pm<->pm collision regression — sender in zeon hits zeon-pm, not drafter-pm."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-drafter-pm", "pm-claude-code", "project-drafter")
        await self._register(pm, "sid-zeon-pm", "pm-claude-code", "project-zeon")
        await self._register(pm, "sid-zeon-head", "backend-head", "project-zeon")
        self._bias_preference_to(pm, "sid-drafter-pm")

        await pm.notify(
            from_peer="backend-head",
            from_peer_id="sid-zeon-head",
            to_peer="pm-claude-code",
            text="status",
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-zeon-pm"

    async def test_unresolved_sender_non_bypass_blocked(self, mock_message_router):
        """T3/DoD7: None from_obj + non-bypass -> blocked (closes early-return-on-None)."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-b", "peer-b", "staging")
        # 'ghost-sender' is not registered; bypass defaults False.
        with pytest.raises(ValueError, match="Circle boundary"):
            await pm.query("ghost-sender", "peer-b", "hi")

    async def test_unresolved_sender_with_bypass_allowed(self, mock_message_router):
        """Guard hardening does not affect bypass callers (CLI auto-bypass)."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-b", "peer-b", "staging")
        result = await pm.query("cli", "peer-b", "hi", bypass_circle=True)
        assert result == "mock response"

    async def test_unresolved_sender_orchestrator_target_allowed(self, mock_message_router):
        """beads-8lzb: prod repro — unresolved sender (telegram-gateway relaying a
        user message, no from_peer_id, non-bypass) reaches an ORCHESTRATOR target
        (director). Service/global targets are reachable from outside the circle
        system by design; the hqvm DoD7 guard must NOT cut this legit channel."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-dir", "director", "global", role=PeerRole.ORCHESTRATOR)

        await pm.notify("telegram-gateway", "director", "user message")

        mock_message_router.send_notification.assert_called_once()
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-dir"

    async def test_unresolved_sender_service_target_allowed(self, mock_message_router):
        """beads-8lzb: same invariant for a SERVICE target (telegram/brain-admin) —
        bypasses_circles is True for SERVICE too, so an unresolved non-bypass sender
        must pass the guard."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-svc", "brain-admin", "global", role=PeerRole.SERVICE)

        await pm.notify("telegram-gateway", "brain-admin", "secret filled")

        mock_message_router.send_notification.assert_called_once()
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-svc"

    async def test_unresolved_sender_project_target_blocked(self, mock_message_router):
        """beads-8lzb / hqvm DoD7 (refined): unresolved non-bypass sender still
        BLOCKED for a project-scoped (role=AGENT) target — the real leak case.
        Explicit analog of test_unresolved_sender_non_bypass_blocked, kept here so
        the pass/block boundary is visible side by side."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-w", "project-worker", "project-zeon")
        with pytest.raises(ValueError, match="Circle boundary"):
            await pm.notify("ghost-sender", "project-worker", "leak attempt")

    async def test_bypass_sender_not_scoped_to_its_circle(self, mock_message_router):
        """T4: service/orchestrator sender is NOT scoped — reaches other circles."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-svc", "telegram", "global", role=PeerRole.SERVICE)
        await self._register(pm, "sid-w", "worker", "dev")

        await pm.notify("telegram", "worker", "hi", from_peer_id="sid-svc")
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-w"

    async def test_explicit_circle_overrides_sender_scope(self, mock_message_router):
        """T5: explicit circle= wins over sender-circle auto-scope."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-a", "myproject", "teamA")
        await self._register(pm, "sid-b", "myproject", "teamB")
        await self._register(pm, "sid-dir", "director", "global", role=PeerRole.ORCHESTRATOR)

        await pm.notify(
            "director", "myproject", "hi", circle="teamB", from_peer_id="sid-dir"
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-b"

    async def test_intent_ack_reply_scopes_to_receiver_circle(self, mock_message_router):
        """T7: intent-ACK (receiver-LLM replies to the original sender's NAME) is
        fixed transitively — the authenticated receiver's reply scopes to its own
        circle, hitting the correct same-circle sender, not a foreign namesake."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-abt-worker", "backend-worker", "circle-abt")
        await self._register(pm, "sid-zeon-worker", "backend-worker", "circle-zeon")
        await self._register(pm, "sid-zeon-head", "backend-head", "circle-zeon")
        # Bias so the wrong-circle (abt) worker would win a blind preference pick.
        self._bias_preference_to(pm, "sid-abt-worker")

        # zeon-head replies (intent-ACK) to "backend-worker" by name, no explicit
        # circle, authenticated by its own peer_id.
        await pm.notify(
            from_peer="backend-head",
            from_peer_id="sid-zeon-head",
            to_peer="backend-worker",
            text="ACK notif-x",
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-zeon-worker"

    async def test_notify_to_peer_id_targets_exactly(self, mock_message_router):
        """DoD6 plumbing: notify(to_peer_id=...) resolves the exact peer, no ambiguity."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-abt-worker", "backend-worker", "circle-abt")
        await self._register(pm, "sid-zeon-worker", "backend-worker", "circle-zeon")
        await self._register(pm, "sid-zeon-head", "backend-head", "circle-zeon")
        self._bias_preference_to(pm, "sid-abt-worker")

        # AUTO-ACK reverse route: zeon-head ACKs back to the exact original sender id.
        await pm.notify(
            from_peer="backend-head",
            from_peer_id="sid-zeon-head",
            to_peer="backend-worker",
            to_peer_id="sid-zeon-worker",
            text="[AUTO-ACK]",
            bypass_circle=True,
        )
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-zeon-worker"


# ---------------------------------------------------------------------------
# Reverse-route receipt (AUTO-ACK / NACK) anti-leak — beads-fqus
# The receipt is addressed back to the ORIGINAL sender. When the original
# sender's authenticated peer_id was threaded forward (to_peer_id present) the
# receipt hits it exactly. When it was NOT (forward carried no from_peer_id) the
# bypass_circle receipt must NOT be blind-delivered to a foreign-circle namesake
# of an ambiguous display_name — better to drop the best-effort receipt than to
# leak it across circles.
# ---------------------------------------------------------------------------


class TestReverseRouteReceiptCollision:
    @staticmethod
    async def _register(pm, sid, name, circle, role=PeerRole.AGENT):
        peer = Peer(
            peer_id=sid, display_name=name, path=f"/{name}",
            machine="localhost", circle=circle, role=role,
        )
        await pm.register_peer(peer)

    @staticmethod
    def _bias_preference_to(pm, *winning_sids):
        transport = MagicMock()
        transport.is_connected = lambda sid: sid in winning_sids
        pm._transport = transport

    async def test_reverse_receipt_with_to_peer_id_hits_exact_sender(
        self, mock_message_router
    ):
        """KEY repro (authenticated): drafter-pm + zeon-pm both online; reverse
        AUTO-ACK with the original sender's to_peer_id hits drafter-pm exactly,
        never the biased zeon-pm namesake."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-drafter-pm", "pm-claude-code", "project-drafter")
        await self._register(pm, "sid-zeon-pm", "pm-claude-code", "project-zeon")
        await self._register(pm, "sid-drafter-gsd", "gsd-dev", "project-drafter")
        self._bias_preference_to(pm, "sid-zeon-pm")  # wrong namesake would win

        await pm.notify(
            from_peer="gsd-dev",
            to_peer="pm-claude-code",
            to_peer_id="sid-drafter-pm",
            text="[AUTO-ACK] notif-x delivered: queued",
            bypass_circle=True,
            reverse_receipt=True,
        )
        mock_message_router.send_notification.assert_called_once()
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-drafter-pm"

    async def test_reverse_receipt_without_to_peer_id_drops_on_ambiguous_sender(
        self, mock_message_router
    ):
        """KEY repro (unauthenticated): forward threaded no from_peer_id, so the
        reverse receipt has no to_peer_id. With two pm namesakes the daemon must
        NOT blind-deliver to the biased foreign-circle one — it drops the receipt
        rather than leak cross-circle."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-drafter-pm", "pm-claude-code", "project-drafter")
        await self._register(pm, "sid-zeon-pm", "pm-claude-code", "project-zeon")
        await self._register(pm, "sid-drafter-gsd", "gsd-dev", "project-drafter")
        self._bias_preference_to(pm, "sid-zeon-pm")  # the leak target

        await pm.notify(
            from_peer="gsd-dev",
            to_peer="pm-claude-code",
            text="[AUTO-ACK] notif-x delivered: queued",
            bypass_circle=True,
            reverse_receipt=True,
        )
        # Dropped — no blind cross-circle delivery to the foreign namesake.
        mock_message_router.send_notification.assert_not_called()

    async def test_reverse_nack_without_to_peer_id_drops_on_ambiguous_sender(
        self, mock_message_router
    ):
        """Same anti-leak invariant for an AUTO-NACK reverse receipt."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-drafter-pm", "pm-claude-code", "project-drafter")
        await self._register(pm, "sid-zeon-pm", "pm-claude-code", "project-zeon")
        self._bias_preference_to(pm, "sid-zeon-pm")

        await pm.notify(
            from_peer="backend-head",
            to_peer="pm-claude-code",
            text="[AUTO-NACK] notif-x failed: injection failed",
            bypass_circle=True,
            reverse_receipt=True,
        )
        mock_message_router.send_notification.assert_not_called()

    async def test_reverse_receipt_unique_sender_delivered_without_to_peer_id(
        self, mock_message_router
    ):
        """Don't over-drop: a legit cross-circle bypass receipt to a UNIQUE-named
        sender (worker -> director) still delivers even without to_peer_id —
        there is no ambiguity to leak through."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(
            pm, "sid-dir", "director", "global", role=PeerRole.ORCHESTRATOR
        )
        await self._register(pm, "sid-w", "worker", "project-x")

        await pm.notify(
            from_peer="worker",
            to_peer="director",
            text="[AUTO-ACK] notif-x delivered: queued",
            bypass_circle=True,
            reverse_receipt=True,
        )
        mock_message_router.send_notification.assert_called_once()
        _, kwargs = mock_message_router.send_notification.call_args
        assert kwargs["to_session_id"] == "sid-dir"


# ---------------------------------------------------------------------------
# broadcast threads the authenticated from_peer_id — beads-fqus
# Without it, a broadcast's receiver cannot address its AUTO-ACK back to the
# exact original sender and the receipt misroutes by ambiguous display_name.
# ---------------------------------------------------------------------------


class TestBroadcastThreadsFromPeerId:
    @staticmethod
    async def _register(pm, sid, name, circle, role=PeerRole.AGENT):
        peer = Peer(
            peer_id=sid, display_name=name, path=f"/{name}",
            machine="localhost", circle=circle, role=role,
        )
        await pm.register_peer(peer)

    async def test_broadcast_passes_resolved_from_peer_id_to_router(
        self, mock_message_router
    ):
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-sender", "backend-worker", "circle-zeon")
        await self._register(pm, "sid-other", "backend-head", "circle-zeon")

        await pm.broadcast(
            from_peer="backend-worker",
            from_peer_id="sid-sender",
            text="heads up",
        )
        mock_message_router.broadcast.assert_called_once()
        _, kwargs = mock_message_router.broadcast.call_args
        assert kwargs.get("from_peer_id") == "sid-sender"


# ---------------------------------------------------------------------------
# Peer model -- role field
# ---------------------------------------------------------------------------


class TestPeerRoleField:
    """Tests for role field in Peer model."""

    def test_peer_default_role_is_agent(self):
        peer = Peer(name="test", path="/test", machine="localhost")
        assert peer.role == PeerRole.AGENT

    def test_peer_role_in_to_dict(self):
        peer = Peer(name="test", path="/test", machine="localhost", role=PeerRole.SERVICE)
        data = peer.to_dict()
        assert data["role"] == "service"

    def test_peer_bypasses_circles_property(self):
        agent = Peer(name="a", path="/a", machine="m", role=PeerRole.AGENT)
        service = Peer(name="s", path="/s", machine="m", role=PeerRole.SERVICE)
        orchestrator = Peer(name="o", path="/o", machine="m", role=PeerRole.ORCHESTRATOR)
        human = Peer(name="h", path="/h", machine="m", role=PeerRole.HUMAN)

        assert not agent.bypasses_circles
        assert service.bypasses_circles
        assert orchestrator.bypasses_circles
        assert human.bypasses_circles


# ---------------------------------------------------------------------------
# Role-based circle bypass
# ---------------------------------------------------------------------------


class TestRoleBasedCircleBypass:
    """Tests for role-based automatic circle bypass."""

    @staticmethod
    async def _register(
        pm: PeerRegistry, session_id: str, name: str, circle: str,
        role: PeerRole = PeerRole.AGENT,
    ) -> None:
        peer = Peer(
            peer_id=session_id,
            display_name=name,
            path=f"/{name}",
            machine="localhost",
            circle=circle,
            role=role,
        )
        await pm.register_peer(peer)

    async def test_service_role_bypasses_circle(self, mock_message_router):
        """Service peer can query agent in a different circle without bypass_circle."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-svc", "telegram", "default", role=PeerRole.SERVICE)
        await self._register(pm, "sid-agent", "worker", "dev")

        result = await pm.query("telegram", "worker", "hello")
        assert result == "mock response"

    async def test_orchestrator_role_bypasses_circle(self, mock_message_router):
        """Orchestrator peer can query agent in a different circle."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-orch", "orchestrator", "global", role=PeerRole.ORCHESTRATOR)
        await self._register(pm, "sid-agent", "worker", "dev")

        result = await pm.query("orchestrator", "worker", "hello")
        assert result == "mock response"

    async def test_agent_role_does_not_bypass_circle(self, mock_message_router):
        """Agent-to-agent cross-circle query is still blocked."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-a", "peer-a", "dev")
        await self._register(pm, "sid-b", "peer-b", "staging")

        with pytest.raises(ValueError, match="Circle boundary"):
            await pm.query("peer-a", "peer-b", "hello")

    async def test_target_role_bypasses_circle(self, mock_message_router):
        """Agent can query a service peer in a different circle (target bypasses)."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-agent", "worker", "dev")
        await self._register(pm, "sid-svc", "telegram", "default", role=PeerRole.SERVICE)

        result = await pm.query("worker", "telegram", "hello")
        assert result == "mock response"

    async def test_service_notify_cross_circle(self, mock_message_router):
        """Service peer can notify agent in a different circle."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-svc", "slack", "default", role=PeerRole.SERVICE)
        await self._register(pm, "sid-agent", "worker", "dev")

        await pm.notify("slack", "worker", "hi")
        mock_message_router.send_notification.assert_called_once()

    async def test_service_peer_receives_broadcast_cross_circle(self, mock_message_router):
        """Service peer receives broadcasts from agents in other circles."""
        pm = PeerRegistry(config=Config(), message_router=mock_message_router)
        await self._register(pm, "sid-agent", "worker", "dev")
        await self._register(pm, "sid-svc", "telegram", "default", role=PeerRole.SERVICE)
        await self._register(pm, "sid-other", "other-agent", "staging")

        mock_message_router.broadcast = AsyncMock(return_value=["sid-svc"])
        await pm.broadcast("worker", "hello everyone")

        # Service peer should NOT be excluded; staging agent should be excluded
        call_kwargs = mock_message_router.broadcast.call_args[1]
        excluded = call_kwargs["exclude"]
        assert "sid-agent" in excluded  # sender excluded
        assert "sid-svc" not in excluded  # service peer NOT excluded
        assert "sid-other" in excluded  # different-circle agent excluded
