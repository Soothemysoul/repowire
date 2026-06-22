"""beads-k1b3 (q3v5 L2): PeerRegistry hold-queue integration.

A notify to a RESTARTING peer is spooled (durable) rather than delivered or
rejected; a notify to an ONLINE peer is delivered as before; a notify to a
RESTARTING peer whose spool is full surfaces as a TransportError (the route
turns that into a 503 = genuine delivery failure). On reconnect the spool is
flushed FIFO and cleared.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from repowire.config.models import Config
from repowire.daemon import hold_queue
from repowire.daemon.peer_registry import PeerRegistry
from repowire.daemon.websocket_transport import TransportError, WebSocketTransport
from repowire.protocol.peers import Peer, PeerStatus


def _make_registry(tmp_path) -> PeerRegistry:
    transport = MagicMock(spec=WebSocketTransport)
    transport.is_connected = MagicMock(return_value=True)
    router = MagicMock()
    router.send_notification = AsyncMock()
    return PeerRegistry(
        config=Config(),
        message_router=router,
        query_tracker=None,
        transport=transport,
        persistence_path=tmp_path / "sessions.json",
        hold_queue_dir=tmp_path / "holdq",
    )


async def _register(reg, peer_id, name, circle="dev", status=PeerStatus.ONLINE):
    peer = Peer(
        peer_id=peer_id, display_name=name, path=f"/tmp/{name}",
        machine="test", circle=circle, status=status,
    )
    await reg.register_peer(peer)
    await reg.update_peer_status(peer_id, status)
    return peer


async def test_notify_to_online_peer_delivers(tmp_path):
    reg = _make_registry(tmp_path)
    await _register(reg, "repow-dev-sender01", "director", status=PeerStatus.ONLINE)
    await _register(reg, "repow-dev-target01", "pm", status=PeerStatus.ONLINE)

    await reg.notify(from_peer="director", to_peer="pm", text="[#notif-00000001] hi")

    reg._router.send_notification.assert_awaited_once()
    assert hold_queue.count(tmp_path / "holdq", "repow-dev-target01") == 0


async def test_notify_to_restarting_peer_is_held_not_delivered(tmp_path):
    reg = _make_registry(tmp_path)
    await _register(reg, "repow-dev-sender02", "director", status=PeerStatus.ONLINE)
    await _register(reg, "repow-dev-target02", "pm", status=PeerStatus.ONLINE)
    await reg.update_peer_status("repow-dev-target02", PeerStatus.RESTARTING)

    await reg.notify(from_peer="director", to_peer="pm", text="[#notif-00000002] held msg")

    reg._router.send_notification.assert_not_awaited()
    held = hold_queue.read_all(tmp_path / "holdq", "repow-dev-target02")
    assert len(held) == 1
    assert held[0]["text"] == "[#notif-00000002] held msg"
    assert held[0]["from_peer"] == "director"
    assert held[0]["correlation_id"] == "notif-00000002"


async def test_notify_to_restarting_full_spool_raises_transport_error(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOWIRE_HOLDQ_MAX_ENTRIES", "1")
    reg = _make_registry(tmp_path)
    await _register(reg, "repow-dev-sender03", "director", status=PeerStatus.ONLINE)
    await _register(reg, "repow-dev-target03", "pm", status=PeerStatus.ONLINE)
    await reg.update_peer_status("repow-dev-target03", PeerStatus.RESTARTING)

    await reg.notify(from_peer="director", to_peer="pm", text="[#notif-00000003] first")
    with pytest.raises(TransportError):
        await reg.notify(from_peer="director", to_peer="pm", text="[#notif-00000004] overflow")


async def test_flush_hold_queue_delivers_fifo_and_clears(tmp_path):
    reg = _make_registry(tmp_path)
    await _register(reg, "repow-dev-sender04", "director", status=PeerStatus.ONLINE)
    await _register(reg, "repow-dev-target04", "pm", status=PeerStatus.ONLINE)
    await reg.update_peer_status("repow-dev-target04", PeerStatus.RESTARTING)
    await reg.notify(from_peer="director", to_peer="pm", text="[#notif-00000005] one")
    await reg.notify(from_peer="director", to_peer="pm", text="[#notif-00000006] two")

    # peer respawns → ONLINE
    await reg.update_peer_status("repow-dev-target04", PeerStatus.ONLINE)
    delivered = await reg.flush_hold_queue("repow-dev-target04")

    assert delivered == 2
    texts = [c.kwargs["text"] for c in reg._router.send_notification.await_args_list]
    assert texts == ["[#notif-00000005] one", "[#notif-00000006] two"]
    assert hold_queue.count(tmp_path / "holdq", "repow-dev-target04") == 0


async def test_flush_empty_spool_is_noop(tmp_path):
    reg = _make_registry(tmp_path)
    await _register(reg, "repow-dev-target05", "pm", status=PeerStatus.ONLINE)
    delivered = await reg.flush_hold_queue("repow-dev-target05")
    assert delivered == 0
    reg._router.send_notification.assert_not_awaited()


async def test_flush_partial_failure_keeps_undelivered_tail(tmp_path):
    reg = _make_registry(tmp_path)
    await _register(reg, "repow-dev-sender06", "director", status=PeerStatus.ONLINE)
    await _register(reg, "repow-dev-target06", "pm", status=PeerStatus.ONLINE)
    await reg.update_peer_status("repow-dev-target06", PeerStatus.RESTARTING)
    await reg.notify(from_peer="director", to_peer="pm", text="[#notif-00000007] one")
    await reg.notify(from_peer="director", to_peer="pm", text="[#notif-00000008] two")
    await reg.update_peer_status("repow-dev-target06", PeerStatus.ONLINE)

    # first delivery succeeds, second drops (peer vanished again mid-flush)
    reg._router.send_notification.side_effect = [None, TransportError("dropped")]
    delivered = await reg.flush_hold_queue("repow-dev-target06")

    assert delivered == 1
    remaining = hold_queue.read_all(tmp_path / "holdq", "repow-dev-target06")
    assert [e["text"] for e in remaining] == ["[#notif-00000008] two"]
