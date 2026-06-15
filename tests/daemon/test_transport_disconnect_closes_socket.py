"""B-1: disconnect must close the underlying websocket (zombie half-open root).

q2ok director incident root cause: a peer's connection was removed from
``WebSocketTransport._connections`` (disconnect / stale-replace / ghost demote)
WITHOUT closing the underlying websocket. The ws-hook then stayed blocked in its
message-loop (TCP keepalive kept the socket nominally alive), the daemon no
longer knew about it (``is_connected==False``), and the client never triggered
its reconnect-loop — the peer was stuck offline-with-live-WS until a manual
relaunch.

Fix: ``disconnect`` closes the websocket it removes, so the client's
reconnect-loop fires and the peer comes back online on its own.

Fully isolated: a bare ``WebSocketTransport()`` (in-memory, no daemon/port) plus
``AsyncMock(spec=WebSocket)`` fake sockets. No live daemon, socket, or tmux
(q2ok RELEASE-GATE — isolation confirmed by devops-head before run).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket

from repowire.daemon.websocket_transport import WebSocketTransport


@pytest.mark.asyncio
async def test_disconnect_closes_the_removed_websocket():
    """Removing a connection must close its socket so the client reconnects."""
    transport = WebSocketTransport()
    ws = AsyncMock(spec=WebSocket)
    await transport.connect("sid-1", ws)

    removed = await transport.disconnect("sid-1")

    assert removed is True
    assert not transport.is_connected("sid-1")
    ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_disconnect_does_not_close_live_websocket():
    """A disconnect carrying an already-replaced (stale) websocket must skip:
    return False and leave the live connection's socket OPEN."""
    transport = WebSocketTransport()
    old_ws = AsyncMock(spec=WebSocket)
    new_ws = AsyncMock(spec=WebSocket)
    await transport.connect("sid-1", old_ws)
    await transport.connect("sid-1", new_ws)  # replaces old_ws

    # Old handler's finally block disconnects with its own (now stale) socket.
    removed = await transport.disconnect("sid-1", websocket=old_ws)

    assert removed is False
    assert transport.is_connected("sid-1")
    new_ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_swallows_close_error_on_dead_socket():
    """A socket that's already dead (close raises) must not propagate — the
    removal still succeeds; close is best-effort cleanup."""
    transport = WebSocketTransport()
    ws = AsyncMock(spec=WebSocket)
    ws.close.side_effect = RuntimeError("socket already closed")
    await transport.connect("sid-1", ws)

    removed = await transport.disconnect("sid-1")  # must not raise

    assert removed is True
    assert not transport.is_connected("sid-1")
