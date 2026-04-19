"""Regression tests for beads-0ym: singleton-guarantee for head/pm roles.

DoD checks:
- 5 parallel spawn_peer calls for a singleton role → one process spawned, all get same display_name.
- 3 parallel spawn_peer calls for a worker role → 3 different peers (workers untouched).
- WS-level: two connects under the same display_name for a singleton role → second rejected 4009.
- ONLINE check: second /spawn for already-online singleton → 200, status="existing", no real spawn.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from httpx_ws import aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport

from repowire.config.models import AgentType, Config, DaemonConfig, SpawnSettings
from repowire.daemon.deps import cleanup_deps, init_deps
from repowire.daemon.message_router import MessageRouter
from repowire.daemon.peer_registry import PeerRegistry
from repowire.daemon.query_tracker import QueryTracker
from repowire.daemon.routes import health, messages, peers, websocket
from repowire.daemon.routes import spawn as spawn_routes
from repowire.daemon.websocket_transport import WebSocketTransport
from repowire.protocol.peers import PeerStatus
from repowire.spawn import SpawnResult


def _make_app(tmp_path: Path, *, with_ws: bool = False) -> tuple[FastAPI, PeerRegistry]:
    cfg = Config(daemon=DaemonConfig(
        spawn=SpawnSettings(
            allowed_commands=["claude"],
            allowed_paths=[str(tmp_path)],
        ),
    ))
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
    if with_ws:
        app.include_router(websocket.router)
    return app, registry


# ---------------------------------------------------------------------------
# D1: Whitelist presence
# ---------------------------------------------------------------------------

class TestSingletonRoleWhitelist:
    def test_default_singleton_roles_present(self) -> None:
        cfg = Config()
        roles = set(cfg.daemon.spawn.singleton_roles)
        for expected in ("backend-head", "frontend-head", "devops-head", "qa-head", "pm", "project-init"):
            assert expected in roles, f"{expected!r} missing from singleton_roles"

    def test_workers_not_in_whitelist(self) -> None:
        cfg = Config()
        roles = set(cfg.daemon.spawn.singleton_roles)
        for worker in ("backend-worker", "frontend-worker", "qa-worker", "devops-worker"):
            assert worker not in roles, f"{worker!r} should NOT be singleton"

    def test_singleton_roles_overridable(self) -> None:
        cfg = Config(daemon=DaemonConfig(spawn=SpawnSettings(
            singleton_roles=["custom-head"],
        )))
        assert "custom-head" in cfg.daemon.spawn.singleton_roles
        assert "devops-head" not in cfg.daemon.spawn.singleton_roles


# ---------------------------------------------------------------------------
# D2: /spawn dedup — already-online singleton
# ---------------------------------------------------------------------------

class TestSingletonSpawnDedup:
    @pytest.mark.asyncio
    async def test_second_spawn_returns_existing_when_online(self, tmp_path) -> None:
        """Second /spawn for an already-online singleton returns status='existing'."""
        app, registry = _make_app(tmp_path)
        project_path = tmp_path / "devops-head"
        project_path.mkdir()

        canonical_name = "devops-head-claude-code"
        mock_result = SpawnResult(
            display_name=canonical_name,
            tmux_session="default:devops-head",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch("repowire.daemon.routes.spawn.spawn_peer", return_value=mock_result) as mock_spawn:
                    # First spawn
                    r1 = await client.post("/spawn", json={
                        "path": str(project_path),
                        "command": "claude",
                        "circle": "default",
                        "wait_for_ready": False,
                    })
                    assert r1.status_code == 200

                    # Simulate peer coming online via allocate_and_register
                    await registry.allocate_and_register(
                        circle="default",
                        backend=AgentType.CLAUDE_CODE,
                        path=str(project_path),
                    )

                    # Second spawn — should return existing, NOT call spawn_peer again
                    r2 = await client.post("/spawn", json={
                        "path": str(project_path),
                        "command": "claude",
                        "circle": "default",
                        "wait_for_ready": False,
                    })
                    assert r2.status_code == 200
                    data = r2.json()
                    assert data["status"] == "existing"
                    assert data["display_name"] == canonical_name
                    # spawn_peer was called exactly once
                    assert mock_spawn.call_count == 1
        finally:
            cleanup_deps()

    @pytest.mark.asyncio
    async def test_5_parallel_singleton_spawns_one_process(self, tmp_path) -> None:
        """5 concurrent spawn requests for a singleton role → exactly one real spawn."""
        app, registry = _make_app(tmp_path)
        project_path = tmp_path / "devops-head"
        project_path.mkdir()

        canonical_name = "devops-head-claude-code"
        spawn_call_count = 0

        async def fake_ready() -> None:
            await asyncio.sleep(0.05)
            registry._fire_spawn_event(canonical_name)

        original_spawn = __import__("repowire.spawn", fromlist=["spawn_peer"]).spawn_peer

        def counting_spawn(*args, **kwargs):
            nonlocal spawn_call_count
            spawn_call_count += 1
            return SpawnResult(
                display_name=canonical_name,
                tmux_session="default:devops-head",
            )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch("repowire.daemon.routes.spawn.spawn_peer", side_effect=counting_spawn):
                    fire_task = asyncio.create_task(fake_ready())

                    results = await asyncio.gather(*[
                        client.post("/spawn", json={
                            "path": str(project_path),
                            "command": "claude",
                            "circle": "default",
                            "wait_for_ready": True,
                            "ready_timeout_ms": 5000,
                        })
                        for _ in range(5)
                    ])

                    await fire_task

            statuses = [r.status_code for r in results]
            assert all(s == 200 for s in statuses), f"Some spawns failed: {statuses}"

            display_names = {r.json()["display_name"] for r in results}
            assert display_names == {canonical_name}, "All callers must get the same display_name"

            # At most one real process spawned
            assert spawn_call_count == 1, (
                f"Expected exactly 1 real spawn, got {spawn_call_count}. "
                "Singleton dedup failed — multiple processes were spawned."
            )
        finally:
            cleanup_deps()

    @pytest.mark.asyncio
    async def test_worker_spawns_are_not_deduplicated(self, tmp_path) -> None:
        """3 parallel spawns for a worker role → 3 distinct processes (no dedup)."""
        app, registry = _make_app(tmp_path)
        project_path = tmp_path / "backend-worker"
        project_path.mkdir()

        call_count = 0

        def counting_spawn(config):
            nonlocal call_count
            call_count += 1
            return SpawnResult(
                display_name=f"backend-worker-claude-code",
                tmux_session=f"default:backend-worker-{call_count}",
            )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch("repowire.daemon.routes.spawn.spawn_peer", side_effect=counting_spawn):
                    results = await asyncio.gather(*[
                        client.post("/spawn", json={
                            "path": str(project_path),
                            "command": "claude",
                            "circle": "default",
                            "wait_for_ready": False,
                        })
                        for _ in range(3)
                    ])

            statuses = [r.status_code for r in results]
            assert all(s == 200 for s in statuses), f"Worker spawns failed: {statuses}"
            # All 3 spawn_peer calls went through
            assert call_count == 3, (
                f"Expected 3 real spawns for worker, got {call_count}. "
                "Worker roles must NOT be deduplicated."
            )
        finally:
            cleanup_deps()


# ---------------------------------------------------------------------------
# D4: PeerRegistry — singleton role blocks collision with ValueError
# ---------------------------------------------------------------------------

class TestSingletonRegistryReject:
    @pytest.mark.asyncio
    async def test_singleton_role_raises_on_online_collision(self, tmp_path) -> None:
        """_build_display_name must raise ValueError for singleton collision (not suffix -2)."""
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

        # Register first peer
        await registry.allocate_and_register(
            circle="default",
            backend=AgentType.CLAUDE_CODE,
            path="/home/user/devops-head",
        )

        # Second registration for same singleton path/circle → must raise ValueError
        with pytest.raises(ValueError, match="Singleton role already online"):
            await registry.allocate_and_register(
                circle="default",
                backend=AgentType.CLAUDE_CODE,
                path="/home/user/devops-head",
            )

    @pytest.mark.asyncio
    async def test_worker_role_gets_suffix_not_error(self, tmp_path) -> None:
        """Worker roles must still get '-2' suffix on collision, not ValueError."""
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

        _, name1 = await registry.allocate_and_register(
            circle="default",
            backend=AgentType.CLAUDE_CODE,
            path="/home/user/backend-worker",
        )
        _, name2 = await registry.allocate_and_register(
            circle="default",
            backend=AgentType.CLAUDE_CODE,
            path="/home/user/backend-worker",
        )

        assert name1 == "backend-worker-claude-code"
        assert name2 == "backend-worker-2-claude-code", (
            "Workers must get '-2' suffix, not an error"
        )

    @pytest.mark.asyncio
    async def test_singleton_offline_peer_allows_new_registration(self, tmp_path) -> None:
        """If the singleton peer is OFFLINE, a new peer can take over the name."""
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

        peer_id, name = await registry.allocate_and_register(
            circle="default",
            backend=AgentType.CLAUDE_CODE,
            path="/home/user/devops-head",
        )
        assert name == "devops-head-claude-code"

        # Mark offline
        registry._peers[peer_id].status = PeerStatus.OFFLINE

        # New registration should succeed (clean takeover)
        _, name2 = await registry.allocate_and_register(
            circle="default",
            backend=AgentType.CLAUDE_CODE,
            path="/home/user/devops-head",
        )
        assert name2 == "devops-head-claude-code"


# ---------------------------------------------------------------------------
# D4: WS-level reject with close code 4009
# ---------------------------------------------------------------------------

class TestWebSocketSingletonReject:
    @pytest.mark.asyncio
    async def test_second_ws_connect_for_singleton_rejected_4009(self, tmp_path) -> None:
        """Second WebSocket connect for a singleton role that is ONLINE is rejected with 4009."""
        app, registry = _make_app(tmp_path, with_ws=True)

        try:
            # First peer connects successfully
            async with AsyncClient(
                transport=ASGIWebSocketTransport(app), base_url="http://test"
            ) as client1, aconnect_ws("/ws", client1) as ws1:
                await ws1.send_json({
                    "type": "connect",
                    "display_name": "devops-head",
                    "circle": "default",
                    "backend": "claude-code",
                    "path": "/home/user/devops-head",
                })
                msg1 = json.loads(await ws1.receive_text())
                assert msg1["type"] == "connected"

                # Second peer tries to connect with same singleton role (still online)
                async with AsyncClient(
                    transport=ASGIWebSocketTransport(app), base_url="http://test"
                ) as client2, aconnect_ws("/ws", client2) as ws2:
                    await ws2.send_json({
                        "type": "connect",
                        "display_name": "devops-head",
                        "circle": "default",
                        "backend": "claude-code",
                        "path": "/home/user/devops-head",
                    })
                    msg2 = json.loads(await ws2.receive_text())
                    assert msg2["type"] == "error", f"Expected error, got: {msg2}"
                    assert (
                        "singleton" in msg2["error"].lower()
                        or "already online" in msg2["error"].lower()
                    ), f"Error should mention singleton conflict: {msg2['error']}"
        finally:
            cleanup_deps()


# ---------------------------------------------------------------------------
# D2: PeerRegistry._is_singleton_role
# ---------------------------------------------------------------------------

class TestIsSingletonRole:
    @pytest.mark.asyncio
    async def test_is_singleton_role_matches_config(self, tmp_path) -> None:
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
        assert registry._is_singleton_role("/home/user/devops-head") is True
        assert registry._is_singleton_role("/home/user/pm") is True
        assert registry._is_singleton_role("/home/user/backend-worker") is False
        assert registry._is_singleton_role(None) is False
