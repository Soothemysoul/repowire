"""Tests for q51 sustained-absence threshold + offline_since tracking.

Background: peer_registry's _build_display_name reclaims a display_name
from any OFFLINE peer immediately. Asymmetric WS drops can mark an
ALIVE peer OFFLINE briefly, leading to wrong-peer routing after
takeover.

Fix: Peer.offline_since timestamp + _MIN_OFFLINE_SECONDS_FOR_TAKEOVER=30s
threshold in _build_display_name. Pre-threshold OFFLINE peers fall to
the suffix path (-2/-3) instead of being pruned.

See docs/superpowers/specs/2026-04-25-q51-mesh-routing-investigation.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from repowire.daemon.peer_registry import (
    _MIN_OFFLINE_SECONDS_FOR_TAKEOVER,
    _set_peer_status,
)
from repowire.protocol.peers import Peer, PeerStatus


def _make_peer(**kwargs) -> Peer:
    defaults = dict(
        peer_id="repow-dev-aaa",
        display_name="dev",
        path="/app",
        machine="laptop",
    )
    defaults.update(kwargs)
    return Peer(**defaults)


class TestPeerOfflineSinceField:
    def test_default_offline_since_is_none(self):
        p = _make_peer()
        assert p.offline_since is None

    def test_set_via_constructor(self):
        ts = datetime.now(timezone.utc)
        p = _make_peer(offline_since=ts)
        assert p.offline_since == ts

    def test_to_dict_includes_offline_since(self):
        ts = datetime.now(timezone.utc)
        p = _make_peer(offline_since=ts)
        d = p.to_dict()
        assert d["offline_since"] == ts.isoformat()

    def test_to_dict_offline_since_none(self):
        p = _make_peer()
        d = _make_peer().to_dict()
        assert d["offline_since"] is None


class TestSetPeerStatus:
    def test_online_to_offline_stamps_offline_since(self):
        p = _make_peer(status=PeerStatus.ONLINE)
        assert p.offline_since is None
        _set_peer_status(p, PeerStatus.OFFLINE)
        assert p.status == PeerStatus.OFFLINE
        assert p.offline_since is not None
        # Timestamp is recent
        age = (datetime.now(timezone.utc) - p.offline_since).total_seconds()
        assert age < 1.0

    def test_offline_to_online_clears_offline_since(self):
        ts = datetime.now(timezone.utc) - timedelta(seconds=10)
        p = _make_peer(status=PeerStatus.OFFLINE, offline_since=ts)
        _set_peer_status(p, PeerStatus.ONLINE)
        assert p.status == PeerStatus.ONLINE
        assert p.offline_since is None

    def test_offline_to_busy_clears_offline_since(self):
        ts = datetime.now(timezone.utc)
        p = _make_peer(status=PeerStatus.OFFLINE, offline_since=ts)
        _set_peer_status(p, PeerStatus.BUSY)
        assert p.status == PeerStatus.BUSY
        assert p.offline_since is None

    def test_re_offline_preserves_original_timestamp(self):
        # Critical for q51 takeover semantics: if a peer is briefly
        # demoted then re-confirmed OFFLINE, the threshold must count
        # from the FIRST drop, not the latest re-confirm.
        ts = datetime.now(timezone.utc) - timedelta(seconds=20)
        p = _make_peer(status=PeerStatus.OFFLINE, offline_since=ts)
        _set_peer_status(p, PeerStatus.OFFLINE)
        # Original timestamp preserved
        assert p.offline_since == ts

    def test_offline_with_no_existing_timestamp_stamps_now(self):
        p = _make_peer(status=PeerStatus.OFFLINE, offline_since=None)
        _set_peer_status(p, PeerStatus.OFFLINE)
        # Backward compat: existing OFFLINE peer without timestamp gets one
        assert p.offline_since is not None

    def test_status_transition_updates_last_seen(self):
        p = _make_peer(status=PeerStatus.ONLINE)
        old_last_seen = p.last_seen
        _set_peer_status(p, PeerStatus.OFFLINE)
        # last_seen advanced
        assert p.last_seen != old_last_seen


class TestThresholdConstant:
    def test_threshold_is_30_seconds(self):
        # Documented value — guards against accidental change
        assert _MIN_OFFLINE_SECONDS_FOR_TAKEOVER == 30.0


# --- Integration: _build_display_name with the threshold ---


@pytest.mark.asyncio
async def test_build_display_name_defers_takeover_for_recent_offline(monkeypatch, tmp_path):
    """Pre-threshold OFFLINE peer keeps its name; new peer gets -2 suffix."""
    from unittest.mock import MagicMock
    from repowire.daemon.peer_registry import PeerRegistry
    from repowire.config.models import AgentType, Config

    reg = PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        persistence_path=tmp_path / "sessions.json",
    )

    # Establish a peer that has been OFFLINE for 10 seconds (< 30s threshold)
    existing = _make_peer(
        peer_id="repow-global-old123",
        display_name="director-claude-code",
        circle="global",
        status=PeerStatus.OFFLINE,
        offline_since=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    async with reg._lock:
        reg._peers[existing.peer_id] = existing

    # New peer wants the same display_name
    async with reg._lock:
        new_name = reg._build_display_name(
            path="/agents/director",
            circle="global",
            backend=AgentType.CLAUDE_CODE,
        )

    # Existing peer NOT pruned (still in registry)
    assert "repow-global-old123" in reg._peers
    # New peer got the -2 suffix because threshold deferred takeover
    assert new_name.endswith("-2"), f"expected -2 suffix, got {new_name}"


@pytest.mark.asyncio
async def test_build_display_name_takes_over_after_threshold(monkeypatch, tmp_path):
    """OFFLINE > 30s peer is pruned and name reclaimed (existing behavior preserved)."""
    from unittest.mock import MagicMock
    from repowire.daemon.peer_registry import PeerRegistry
    from repowire.config.models import AgentType, Config

    reg = PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        persistence_path=tmp_path / "sessions.json",
    )

    existing = _make_peer(
        peer_id="repow-global-old123",
        display_name="director-claude-code",
        circle="global",
        status=PeerStatus.OFFLINE,
        offline_since=datetime.now(timezone.utc) - timedelta(seconds=60),
    )
    async with reg._lock:
        reg._peers[existing.peer_id] = existing

    async with reg._lock:
        new_name = reg._build_display_name(
            path="/agents/director",
            circle="global",
            backend=AgentType.CLAUDE_CODE,
        )

    # Existing peer pruned
    assert "repow-global-old123" not in reg._peers
    # Name reclaimed cleanly (no suffix)
    assert new_name == "director-claude-code"


@pytest.mark.asyncio
async def test_build_display_name_legacy_peer_no_offline_since(tmp_path):
    """Peers without offline_since (legacy) are treated as old enough."""
    from unittest.mock import MagicMock
    from repowire.daemon.peer_registry import PeerRegistry
    from repowire.config.models import AgentType, Config

    reg = PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        persistence_path=tmp_path / "sessions.json",
    )

    legacy = _make_peer(
        peer_id="repow-global-legacy",
        display_name="director-claude-code",
        circle="global",
        status=PeerStatus.OFFLINE,
        offline_since=None,  # legacy peer pre-q51 fix
    )
    async with reg._lock:
        reg._peers[legacy.peer_id] = legacy

    async with reg._lock:
        new_name = reg._build_display_name(
            path="/agents/director",
            circle="global",
            backend=AgentType.CLAUDE_CODE,
        )

    # Legacy peer pruned (backward compat — no threshold blocks it)
    assert "repow-global-legacy" not in reg._peers
    assert new_name == "director-claude-code"
