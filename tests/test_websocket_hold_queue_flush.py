"""beads-k1b3 (q3v5 L2): the WS-reconnect path flushes a peer's hold-queue.

End-to-end: a peer is held (spooled) while RESTARTING; when it reconnects over
the WebSocket the daemon delivers the held notify over the fresh connection and
clears the spool. No mocks — the flushed frame arrives on the reconnected ws.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import AsyncClient
from httpx_ws import aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport

from repowire.config.models import Config, DaemonConfig
from repowire.daemon import hold_queue
from repowire.daemon.deps import cleanup_deps, init_deps
from repowire.daemon.message_router import MessageRouter
from repowire.daemon.peer_registry import PeerRegistry
from repowire.daemon.query_tracker import QueryTracker
from repowire.daemon.routes import health, messages, peers, websocket
from repowire.daemon.websocket_transport import WebSocketTransport
from repowire.protocol.peers import PeerStatus

_PATH = "/tmp/holdflush"


def _make_app(tmp_path: Path):
    cfg = Config(daemon=DaemonConfig())
    transport = WebSocketTransport()
    tracker = QueryTracker()
    router = MessageRouter(transport=transport, query_tracker=tracker)
    registry = PeerRegistry(
        config=cfg,
        message_router=router,
        query_tracker=tracker,
        transport=transport,
        persistence_path=tmp_path / "sessions.json",
        hold_queue_dir=tmp_path / "holdq",
    )
    registry._events_path = tmp_path / "events.json"
    registry._events.clear()
    app_state = SimpleNamespace(
        config=cfg, transport=transport, query_tracker=tracker,
        message_router=router, peer_registry=registry, relay_mode=False,
    )
    init_deps(cfg, registry, app_state)
    app = FastAPI()
    app.include_router(health.router)
    app.include_router(peers.router)
    app.include_router(messages.router)
    app.include_router(websocket.router)
    return app, registry


_CONNECT_MSG = {
    "type": "connect",
    "display_name": "pm",
    "circle": "default",
    "backend": "claude-code",
    "path": _PATH,
}


async def test_reconnect_flushes_held_notify(tmp_path):
    app, registry = _make_app(tmp_path)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app), base_url="http://test"
    ) as client:
        # First connect → learn the stable peer_id, then disconnect.
        async with aconnect_ws("/ws", client) as ws:
            await ws.send_json(_CONNECT_MSG)
            resp = json.loads(await ws.receive_text())
            session_id = resp["session_id"]

        # Peer announces it is restarting; a notify lands in the hold window.
        await registry.update_peer_status(session_id, PeerStatus.RESTARTING)
        hold_queue.enqueue(
            tmp_path / "holdq",
            session_id,
            {
                "correlation_id": "notif-deadbeef",
                "from_peer": "director",
                "from_peer_id": "repow-global-aaaa1111",
                "from_peer_role": "orchestrator",
                "text": "[#notif-deadbeef] held while restarting",
                "interrupt": False,
            },
            now=1000.0,
        )
        assert hold_queue.count(tmp_path / "holdq", session_id) == 1

        # Reconnect (same path → same peer_id) → daemon flushes the spool over
        # the fresh ws. The held notify arrives as a notify frame.
        async with aconnect_ws("/ws", client) as ws2:
            await ws2.send_json(_CONNECT_MSG)
            resp2 = json.loads(await ws2.receive_text())
            assert resp2["session_id"] == session_id

            frame = json.loads(await ws2.receive_text())
            assert frame["type"] == "notify"
            assert frame["text"] == "[#notif-deadbeef] held while restarting"
            assert frame["from_peer"] == "director"

        # Spool cleared after a successful flush.
        assert hold_queue.count(tmp_path / "holdq", session_id) == 0

    cleanup_deps()
