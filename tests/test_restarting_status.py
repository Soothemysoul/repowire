"""beads-k1b3 (q3v5 L2): PeerStatus.RESTARTING — a subordinate that is
self-restarting on context-overflow is neither a live ONLINE/BUSY peer nor a
genuinely-dead OFFLINE one. It is a known-transient state: the daemon holds
incoming notifies instead of rejecting them, and the watchdog grants a longer
grace before escalating.

This module covers the protocol enum + the status-transition bookkeeping in
``_set_peer_status``. The /session/update route acceptance lives alongside the
other route tests; the hold-queue + watchdog behaviour have their own modules.
"""

from __future__ import annotations

from datetime import datetime, timezone

from repowire.protocol.peers import Peer, PeerStatus


def _peer(status: PeerStatus = PeerStatus.ONLINE) -> Peer:
    return Peer(
        peer_id="repow-dev-restart01",
        display_name="pm-claude-code",
        path="/tmp/pm",
        machine="test",
        status=status,
    )


def test_restarting_enum_value():
    assert PeerStatus.RESTARTING.value == "restarting"
    assert PeerStatus("restarting") is PeerStatus.RESTARTING


def test_set_status_restarting_clears_offline_and_busy_since():
    """RESTARTING is a live-ish transient: it must clear both offline_since
    (peer is NOT dead) and busy_since (it is NOT mid-turn)."""
    from repowire.daemon.peer_registry import _set_peer_status

    peer = _peer(status=PeerStatus.BUSY)
    peer.busy_since = datetime.now(timezone.utc)
    peer.offline_since = None

    _set_peer_status(peer, PeerStatus.RESTARTING)

    assert peer.status is PeerStatus.RESTARTING
    assert peer.busy_since is None
    assert peer.offline_since is None
    assert peer.last_seen is not None


def test_set_status_online_after_restarting():
    """RESTARTING → ONLINE (respawn / WS-reconnect) is a normal live transition."""
    from repowire.daemon.peer_registry import _set_peer_status

    peer = _peer(status=PeerStatus.RESTARTING)
    _set_peer_status(peer, PeerStatus.ONLINE)

    assert peer.status is PeerStatus.ONLINE
    assert peer.offline_since is None
    assert peer.busy_since is None


def test_to_dict_serializes_restarting():
    peer = _peer(status=PeerStatus.RESTARTING)
    assert peer.to_dict()["status"] == "restarting"
