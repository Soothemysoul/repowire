"""Tests for daemon HTTP routes (peers, messages, events)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from repowire.config.models import Config
from repowire.daemon.deps import cleanup_deps, init_deps
from repowire.daemon.message_router import MessageRouter
from repowire.daemon.peer_registry import PeerRegistry
from repowire.daemon.query_tracker import QueryTracker
from repowire.daemon.routes import health, messages, peers
from repowire.daemon.routes import spawn as spawn_routes
from repowire.daemon.websocket_transport import WebSocketTransport


def _make_test_app(tmp_path: Path):
    """Build minimal app with deps initialized (no lifespan needed)."""
    cfg = Config()
    transport = WebSocketTransport()
    tracker = QueryTracker()
    router = MessageRouter(transport=transport, query_tracker=tracker)
    registry = PeerRegistry(
        config=cfg,
        message_router=router,
        query_tracker=tracker,
        transport=transport,
        persistence_path=tmp_path / "sessions.json",
    )
    # Override events path to avoid loading real events
    registry._events_path = tmp_path / "events.json"
    registry._events.clear()

    app_state = SimpleNamespace(
        config=cfg,
        transport=transport,
        query_tracker=tracker,
        message_router=router,
        peer_registry=registry,
        relay_mode=False,
    )
    init_deps(cfg, registry, app_state)

    app = FastAPI()
    app.include_router(health.router)
    app.include_router(peers.router)
    app.include_router(messages.router)
    app.include_router(spawn_routes.router)
    return app


@pytest.fixture
async def client(tmp_path):
    """Async HTTP test client with deps initialized."""
    app = _make_test_app(tmp_path)
    t = ASGITransport(app=app)
    async with AsyncClient(transport=t, base_url="http://test") as c:
        yield c
    cleanup_deps()


# -- Health --


class TestHealth:
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# -- Peers --


class TestPeers:
    async def test_list_peers_empty(self, client):
        r = await client.get("/peers")
        assert r.status_code == 200
        assert r.json()["peers"] == []

    async def test_register_peer(self, client):
        r = await client.post("/peers", json={
            "name": "testpeer",
            "path": "/tmp/testpeer",
            "circle": "default",
            "backend": "claude-code",
        })
        assert r.status_code == 200
        name = r.json()["display_name"]
        assert name == "testpeer-claude-code"

        r = await client.get("/peers")
        peers = r.json()["peers"]
        assert len(peers) == 1
        assert peers[0]["display_name"] == name

    async def test_register_peer_with_pane_id(self, client):
        r = await client.post("/peers", json={
            "name": "panepeer",
            "path": "/tmp/panepeer",
            "circle": "default",
            "backend": "claude-code",
            "pane_id": "%77",
        })
        assert r.status_code == 200

        r = await client.get("/peers/by-pane/%2577")
        assert r.status_code == 200
        assert r.json()["display_name"] == "panepeer-claude-code"

    async def test_get_peer_by_name(self, client):
        r = await client.post("/peers", json={
            "name": "mypeer",
            "path": "/tmp/mypeer",
            "circle": "default",
            "backend": "claude-code",
        })
        name = r.json()["display_name"]
        r = await client.get(f"/peers/{name}")
        assert r.status_code == 200
        assert r.json()["display_name"] == name

    async def test_get_peer_not_found(self, client):
        r = await client.get("/peers/nonexistent")
        assert r.status_code == 404

    async def test_delete_peer(self, client):
        r = await client.post("/peers", json={
            "name": "delpeer",
            "path": "/tmp/delpeer",
            "circle": "default",
            "backend": "claude-code",
        })
        name = r.json()["display_name"]
        r = await client.delete(f"/peers/{name}")
        assert r.status_code == 200

        r = await client.get(f"/peers/{name}")
        assert r.status_code == 404

    async def test_set_description(self, client):
        r = await client.post("/peers", json={
            "name": "descpeer",
            "path": "/tmp/descpeer",
            "circle": "default",
            "backend": "claude-code",
        })
        name = r.json()["display_name"]
        r = await client.post(f"/peers/{name}/description", json={
            "description": "working on tests",
        })
        assert r.status_code == 200

        r = await client.get(f"/peers/{name}")
        assert r.json()["description"] == "working on tests"

    async def test_set_description_by_id_targets_only_that_peer(self, client):
        """beads-uksi: POST /peers/by-id/{peer_id}/description writes to the peer
        with that exact peer_id, never to a same-named peer in another circle."""
        r1 = await client.post("/peers", json={
            "name": "twin", "path": "/tmp/twin-a", "circle": "teamA", "backend": "claude-code",
        })
        id_a = r1.json()["peer_id"]
        r2 = await client.post("/peers", json={
            "name": "twin", "path": "/tmp/twin-b", "circle": "teamB", "backend": "claude-code",
        })
        id_b = r2.json()["peer_id"]
        assert id_a != id_b

        r = await client.post(f"/peers/by-id/{id_a}/description", json={
            "description": "A-work",
        })
        assert r.status_code == 200

        ra = await client.get(f"/peers/{id_a}")
        rb = await client.get(f"/peers/{id_b}")
        assert ra.json()["description"] == "A-work"
        assert rb.json()["description"] == ""  # namesake NOT cross-wired

    async def test_set_description_by_id_not_found(self, client):
        r = await client.post("/peers/by-id/no-such-id/description", json={
            "description": "x",
        })
        assert r.status_code == 404

    async def test_register_duplicate_peer(self, client):
        payload = {
            "name": "dup",
            "path": "/tmp/dup",
            "circle": "default",
            "backend": "claude-code",
        }
        r1 = await client.post("/peers", json=payload)
        name = r1.json()["display_name"]
        r = await client.post("/peers", json=payload)
        assert r.status_code == 200

        r = await client.get("/peers")
        names = [p["display_name"] for p in r.json()["peers"]]
        assert names.count(name) == 1


# -- Events --


class TestEvents:
    async def test_get_events_empty(self, client):
        r = await client.get("/events")
        assert r.status_code == 200
        assert r.json() == []

    async def test_post_chat_turn(self, client):
        r = await client.post("/events/chat", json={
            "peer": "testpeer",
            "role": "user",
            "text": "hello",
        })
        assert r.status_code == 200

        r = await client.get("/events")
        events = r.json()
        assert len(events) == 1
        assert events[0]["type"] == "chat_turn"
        assert events[0]["peer"] == "testpeer"
        assert events[0]["text"] == "hello"

    async def test_chat_turn_with_tool_calls(self, client):
        r = await client.post("/events/chat", json={
            "peer": "testpeer",
            "role": "assistant",
            "text": "Done",
            "tool_calls": [
                {"name": "Bash", "input": "echo hello"},
                {"name": "Read", "input": "auth.py"},
            ],
        })
        assert r.status_code == 200

        r = await client.get("/events")
        events = r.json()
        assert len(events) == 1
        assert events[0]["tool_calls"] == [
            {"name": "Bash", "input": "echo hello"},
            {"name": "Read", "input": "auth.py"},
        ]

    async def test_chat_turn_without_tool_calls(self, client):
        r = await client.post("/events/chat", json={
            "peer": "testpeer",
            "role": "assistant",
            "text": "No tools used",
        })
        assert r.status_code == 200

        r = await client.get("/events")
        events = r.json()
        assert events[0].get("tool_calls") is None

    async def test_events_have_id_and_timestamp(self, client):
        await client.post("/events/chat", json={
            "peer": "p", "role": "user", "text": "hi",
        })
        r = await client.get("/events")
        event = r.json()[0]
        assert "id" in event
        assert "timestamp" in event

    async def test_chat_turn_with_explicit_peer_id(self, client):
        """Chat turn with peer_id passed directly should store it in the event."""
        r = await client.post("/events/chat", json={
            "peer": "testpeer",
            "role": "user",
            "text": "hello",
            "peer_id": "repow-default-abc12345",
        })
        assert r.status_code == 200

        r = await client.get("/events")
        events = r.json()
        assert len(events) == 1
        assert events[0]["peer_id"] == "repow-default-abc12345"

    async def test_chat_turn_resolves_peer_id_from_pane_id(self, client):
        """Chat turn with pane_id should resolve peer_id from registry."""
        from repowire.config.models import AgentType
        from repowire.daemon.deps import get_peer_registry
        registry = get_peer_registry()
        _peer_id, _name = await registry.allocate_and_register(
            circle="default",
            backend=AgentType.CLAUDE_CODE,
            path="/tmp/panepeer",
            pane_id="%99",
        )

        r = await client.post("/events/chat", json={
            "peer": "panepeer",
            "role": "assistant",
            "text": "done",
            "pane_id": "%99",
        })
        assert r.status_code == 200

        r = await client.get("/events")
        events = r.json()
        assert len(events) == 1
        assert events[0]["peer_id"] is not None
        assert events[0]["peer_id"].startswith("repow-")

    async def test_chat_turn_without_peer_id_or_pane_id(self, client):
        """Chat turn without peer_id or pane_id should still work (legacy compat)."""
        r = await client.post("/events/chat", json={
            "peer": "legacypeer",
            "role": "user",
            "text": "old style",
        })
        assert r.status_code == 200

        r = await client.get("/events")
        events = r.json()
        assert len(events) == 1
        assert events[0].get("peer_id") is None


# -- Notify --


class TestNotify:
    async def test_notify_unknown_peer(self, client):
        r = await client.post("/notify", json={
            "from_peer": "sender",
            "to_peer": "nonexistent",
            "text": "hello",
        })
        assert r.status_code == 404

    async def test_notify_peer_without_connection_returns_503(self, client):
        # Register peer via HTTP — no WebSocket connection is ever opened,
        # so transport.send() raises TransportError("No connection ...").
        # The /notify handler must surface that as 503, not 500.
        r = await client.post("/peers", json={
            "name": "ghostpeer",
            "path": "/tmp/ghostpeer",
            "circle": "default",
            "backend": "claude-code",
        })
        assert r.status_code == 200
        name = r.json()["display_name"]

        # Register the sender too (same circle) so the circle guard passes and
        # we actually reach transport.send — which raises because ghostpeer has
        # no WebSocket connection. (beads-hqvm: an unresolved non-bypass sender
        # is now blocked at the circle guard, so the sender must be registered.)
        rs = await client.post("/peers", json={
            "name": "sender",
            "path": "/tmp/sender",
            "circle": "default",
            "backend": "claude-code",
        })
        sender_name = rs.json()["display_name"]

        r = await client.post("/notify", json={
            "from_peer": sender_name,
            "to_peer": name,
            "text": "hello",
        })
        assert r.status_code == 503
        assert "Peer unreachable" in r.json()["detail"]

    async def test_reverse_receipt_to_ambiguous_target_is_dropped(self, client):
        """beads-fqus end-to-end: a reverse_receipt notify with no to_peer_id to an
        ambiguous (cross-circle namesake) target is dropped by the daemon — it
        returns 200 without ever reaching transport (no 503 from a blind delivery
        attempt), and never leaks to a foreign-circle namesake."""
        # Same display_name in two different circles -> ambiguous target.
        for circle in ("project-drafter", "project-zeon"):
            rp = await client.post("/peers", json={
                "name": "pm", "path": "/tmp/pm", "circle": circle,
                "backend": "claude-code",
            })
            assert rp.status_code == 200
            amb_name = rp.json()["display_name"]

        r = await client.post("/notify", json={
            "from_peer": "gsd-dev",
            "to_peer": amb_name,
            "text": "[AUTO-ACK] notif-x delivered: queued",
            "bypass_circle": True,
            "reverse_receipt": True,
        })
        assert r.status_code == 200


# -- Broadcast --


class TestBroadcast:
    async def test_broadcast_no_peers(self, client):
        r = await client.post("/broadcast", json={
            "from_peer": "sender",
            "text": "hello all",
        })
        assert r.status_code == 200
        assert r.json()["sent_to"] == []


# -- Session Update --


class TestSessionUpdate:
    async def test_update_by_peer_name(self, client):
        r = await client.post("/peers", json={
            "name": "statuspeer",
            "path": "/tmp/statuspeer",
            "circle": "default",
            "backend": "claude-code",
        })
        name = r.json()["display_name"]
        r = await client.post("/session/update", json={
            "peer_name": name,
            "status": "busy",
        })
        assert r.status_code == 200

        r = await client.get(f"/peers/{name}")
        assert r.json()["status"] == "busy"

    async def test_update_status_restarting(self, client):
        """beads-k1b3: /session/update accepts the new RESTARTING status so a
        subordinate can announce its context-overflow restart to the daemon."""
        r = await client.post("/peers", json={
            "name": "restartpeer",
            "path": "/tmp/restartpeer",
            "circle": "default",
            "backend": "claude-code",
        })
        name = r.json()["display_name"]
        r = await client.post("/session/update", json={
            "peer_name": name,
            "status": "restarting",
        })
        assert r.status_code == 200

        r = await client.get(f"/peers/{name}")
        assert r.json()["status"] == "restarting"

    async def test_update_status_invalid_rejected(self, client):
        r = await client.post("/peers", json={
            "name": "badstatuspeer",
            "path": "/tmp/badstatuspeer",
            "circle": "default",
            "backend": "claude-code",
        })
        name = r.json()["display_name"]
        r = await client.post("/session/update", json={
            "peer_name": name,
            "status": "bogus",
        })
        assert r.status_code == 400
        assert "restarting" in r.json()["detail"]
