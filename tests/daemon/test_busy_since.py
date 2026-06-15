"""B-3 (part 1): _set_peer_status maintains Peer.busy_since.

A peer that entered BUSY and whose pane then died kept BUSY forever (the Stop
hook that clears BUSY never fired). ``busy_since`` records when a peer entered
BUSY — telemetry the periodic pane-liveness sweep uses to reason about
long-running turns. It is NOT an eviction criterion (a genuinely live long turn
must never be killed); the sweep evicts only on a dead pane.

Pure value-object unit test — no daemon, transport, or tmux.
"""

from __future__ import annotations

from repowire.daemon.peer_registry import _set_peer_status
from repowire.protocol.peers import Peer, PeerStatus


def _peer(status: PeerStatus) -> Peer:
    return Peer(
        peer_id="repow-dev-abc12345",
        display_name="myproject",
        path="/tmp/test",
        machine="test",
        status=status,
    )


def test_entering_busy_stamps_busy_since():
    p = _peer(PeerStatus.ONLINE)
    assert p.busy_since is None
    _set_peer_status(p, PeerStatus.BUSY)
    assert p.busy_since is not None


def test_rebusy_preserves_original_busy_since():
    """Re-confirming BUSY must not reset the timestamp — sweep reasoning needs
    the time of the FIRST BUSY transition, not the latest."""
    p = _peer(PeerStatus.ONLINE)
    _set_peer_status(p, PeerStatus.BUSY)
    first = p.busy_since
    _set_peer_status(p, PeerStatus.BUSY)
    assert p.busy_since == first


def test_leaving_busy_for_online_clears_busy_since():
    p = _peer(PeerStatus.ONLINE)
    _set_peer_status(p, PeerStatus.BUSY)
    _set_peer_status(p, PeerStatus.ONLINE)
    assert p.busy_since is None


def test_leaving_busy_for_offline_clears_busy_since():
    p = _peer(PeerStatus.ONLINE)
    _set_peer_status(p, PeerStatus.BUSY)
    _set_peer_status(p, PeerStatus.OFFLINE)
    assert p.busy_since is None
