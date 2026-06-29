"""Regression tests for beads-99oh: расклинить stuck «restarting» (A1+A2+A3).

The wedge: a peer stuck in RESTARTING (its process is gone, it never came back)
was treated as live, so:
  A1 — /spawn singleton-dedup returned status="existing" / elapsed_ms=0 WITHOUT
       relaunching (fake-online no-op).
  A2 — /kill on it was a pure tmux op with no registry side-effect, so even a
       successful "kill" left the RESTARTING record alive → next spawn no-op'd
       again. A dead pane returned 404 instead of cleaning the registry.
  A3 — the liveness sweep only demoted a stuck RESTARTING → OFFLINE after a
       15-minute cap (900s).

CRITICAL invariant (beads-k1b3): a HEALTHY self-restart (fresh, within the cap)
must STILL be deduped/held — the fix targets STUCK restarts only, it must not
cancel legitimate RESTARTING semantics.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from repowire.config.models import AgentType, Config, DaemonConfig, SpawnSettings
from repowire.daemon.deps import cleanup_deps, init_deps
from repowire.daemon.message_router import MessageRouter
from repowire.daemon.peer_registry import PeerRegistry
from repowire.daemon.query_tracker import QueryTracker
from repowire.daemon.routes import health, messages, peers
from repowire.daemon.routes import spawn as spawn_routes
from repowire.daemon.websocket_transport import WebSocketTransport
from repowire.protocol.peers import PeerStatus
from repowire.spawn import SpawnResult


def _make_app(tmp_path: Path) -> tuple[FastAPI, PeerRegistry]:
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
    return app, registry


def _make_registry(tmp_path: Path) -> PeerRegistry:
    transport = WebSocketTransport()
    tracker = QueryTracker()
    router = MessageRouter(transport=transport, query_tracker=tracker)
    return PeerRegistry(
        config=Config(),
        message_router=router,
        query_tracker=tracker,
        transport=transport,
        persistence_path=tmp_path / "sessions.json",
    )


# ---------------------------------------------------------------------------
# A1 — PeerRegistry.is_restart_stuck: single definition of "stuck restart"
# ---------------------------------------------------------------------------


class TestIsRestartStuck:
    @pytest.mark.asyncio
    async def test_restarting_past_cap_is_stuck(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REPOWIRE_RESTART_PEER_CAP_SEC", "100")
        reg = _make_registry(tmp_path)
        pid, _ = await reg.allocate_and_register(
            circle="default", backend=AgentType.CLAUDE_CODE, path="/x/pm",
        )
        await reg.update_peer_status(pid, PeerStatus.RESTARTING)
        peer = await reg.get_peer(pid)
        peer.restarting_since = datetime.now(timezone.utc) - timedelta(seconds=200)
        assert reg.is_restart_stuck(peer) is True

    @pytest.mark.asyncio
    async def test_fresh_restarting_within_cap_is_not_stuck(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REPOWIRE_RESTART_PEER_CAP_SEC", "100")
        reg = _make_registry(tmp_path)
        pid, _ = await reg.allocate_and_register(
            circle="default", backend=AgentType.CLAUDE_CODE, path="/x/pm",
        )
        await reg.update_peer_status(pid, PeerStatus.RESTARTING)
        peer = await reg.get_peer(pid)
        assert reg.is_restart_stuck(peer) is False

    @pytest.mark.asyncio
    async def test_online_peer_is_not_stuck(self, tmp_path):
        reg = _make_registry(tmp_path)
        pid, _ = await reg.allocate_and_register(
            circle="default", backend=AgentType.CLAUDE_CODE, path="/x/pm",
        )
        peer = await reg.get_peer(pid)
        peer.status = PeerStatus.ONLINE
        assert reg.is_restart_stuck(peer) is False

    @pytest.mark.asyncio
    async def test_restarting_without_timestamp_is_not_stuck(self, tmp_path):
        """A RESTARTING peer with no restarting_since is not demoted — matches
        liveness_tick's existing `restarting_since is not None` guard."""
        reg = _make_registry(tmp_path)
        pid, _ = await reg.allocate_and_register(
            circle="default", backend=AgentType.CLAUDE_CODE, path="/x/pm",
        )
        peer = await reg.get_peer(pid)
        peer.status = PeerStatus.RESTARTING
        peer.restarting_since = None
        assert reg.is_restart_stuck(peer) is False


# ---------------------------------------------------------------------------
# A1 — /spawn singleton-dedup relaunches a STUCK restart, dedups a HEALTHY one
# ---------------------------------------------------------------------------


class TestSpawnDedupVsStuckRestart:
    @pytest.mark.asyncio
    async def test_stuck_restarting_singleton_is_relaunched(self, tmp_path, monkeypatch):
        """A singleton stuck in RESTARTING past the cap → /spawn actually launches
        a process (NOT a status='existing' 0ms no-op)."""
        monkeypatch.setenv("REPOWIRE_RESTART_PEER_CAP_SEC", "100")
        app, registry = _make_app(tmp_path)
        project_path = tmp_path / "devops-head"
        project_path.mkdir()
        canonical = "devops-head-claude-code"
        mock_result = SpawnResult(display_name=canonical, tmux_session="default:devops-head")

        try:
            # Existing peer is stuck in RESTARTING (process gone, never returned).
            pid, _ = await registry.allocate_and_register(
                circle="default", backend=AgentType.CLAUDE_CODE, path=str(project_path),
            )
            await registry.update_peer_status(pid, PeerStatus.RESTARTING)
            registry._peers[pid].restarting_since = (
                datetime.now(timezone.utc) - timedelta(seconds=200)
            )

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch(
                    "repowire.daemon.routes.spawn.spawn_peer", return_value=mock_result
                ) as mock_spawn:
                    r = await client.post("/spawn", json={
                        "path": str(project_path),
                        "command": "claude",
                        "circle": "default",
                        "wait_for_ready": False,
                    })
                    assert r.status_code == 200
                    assert mock_spawn.call_count == 1, (
                        "stuck RESTARTING must be relaunched, not no-op'd"
                    )
                    assert r.json()["status"] != "existing"
        finally:
            cleanup_deps()

    @pytest.mark.asyncio
    async def test_healthy_restarting_singleton_is_deduped(self, tmp_path, monkeypatch):
        """k1b3 invariant: a HEALTHY (fresh, within cap) self-restart is still
        deduped — /spawn returns status='existing', no relaunch."""
        monkeypatch.setenv("REPOWIRE_RESTART_PEER_CAP_SEC", "100")
        app, registry = _make_app(tmp_path)
        project_path = tmp_path / "devops-head"
        project_path.mkdir()

        try:
            pid, _ = await registry.allocate_and_register(
                circle="default", backend=AgentType.CLAUDE_CODE, path=str(project_path),
            )
            await registry.update_peer_status(pid, PeerStatus.RESTARTING)  # fresh

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch(
                    "repowire.daemon.routes.spawn.spawn_peer"
                ) as mock_spawn:
                    r = await client.post("/spawn", json={
                        "path": str(project_path),
                        "command": "claude",
                        "circle": "default",
                        "wait_for_ready": False,
                    })
                    assert r.status_code == 200
                    assert r.json()["status"] == "existing"
                    assert mock_spawn.call_count == 0, (
                        "healthy RESTARTING must NOT be relaunched (k1b3)"
                    )
        finally:
            cleanup_deps()


# ---------------------------------------------------------------------------
# A2 — /kill always demotes the registry record to OFFLINE (200, not 404)
# ---------------------------------------------------------------------------


class TestKillDemotesRegistry:
    @pytest.mark.asyncio
    async def test_kill_dead_pane_returns_200_and_marks_offline(self, tmp_path):
        """Peer has a pane_id but the pane is already dead → /kill cleans the
        registry (OFFLINE) and returns 200, not 404."""
        app, registry = _make_app(tmp_path)
        try:
            pid, _ = await registry.allocate_and_register(
                circle="default", backend=AgentType.CLAUDE_CODE, path="/x/devops-head",
            )
            await registry.update_peer_status(pid, PeerStatus.RESTARTING)
            registry._peers[pid].pane_id = "%99"

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch(
                    "repowire.daemon.routes.spawn.kill_peer_by_pane", return_value=False
                ):
                    r = await client.post("/kill", json={
                        "peer_name": "devops-head-claude-code",
                        "circle": "default",
                    })
            assert r.status_code == 200
            peer = await registry.get_peer(pid)
            assert peer.status is PeerStatus.OFFLINE
        finally:
            cleanup_deps()

    @pytest.mark.asyncio
    async def test_kill_peer_without_pane_returns_200_and_marks_offline(self, tmp_path):
        """A RESTARTING record with no pane_id must be demotable via /kill —
        200 + OFFLINE, not 404."""
        app, registry = _make_app(tmp_path)
        try:
            pid, _ = await registry.allocate_and_register(
                circle="default", backend=AgentType.CLAUDE_CODE, path="/x/devops-head",
            )
            await registry.update_peer_status(pid, PeerStatus.RESTARTING)
            assert registry._peers[pid].pane_id is None

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch(
                    "repowire.daemon.routes.spawn.kill_peer_by_pane"
                ) as mock_kill:
                    r = await client.post("/kill", json={
                        "peer_name": "devops-head-claude-code",
                        "circle": "default",
                    })
                    assert mock_kill.call_count == 0  # no pane to kill
            assert r.status_code == 200
            peer = await registry.get_peer(pid)
            assert peer.status is PeerStatus.OFFLINE
        finally:
            cleanup_deps()

    @pytest.mark.asyncio
    async def test_kill_unknown_peer_still_404(self, tmp_path):
        """A genuinely unknown identifier has nothing to clean → still 404."""
        app, registry = _make_app(tmp_path)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post("/kill", json={
                    "peer_name": "nobody-claude-code",
                    "circle": "default",
                })
            assert r.status_code == 404
        finally:
            cleanup_deps()


# ---------------------------------------------------------------------------
# A3 — restart cap default lowered 900 -> 180s (env override preserved)
# ---------------------------------------------------------------------------


class TestRestartCapDefault:
    def test_default_cap_is_180(self, monkeypatch):
        from repowire.daemon.peer_registry import _restart_peer_cap_sec

        monkeypatch.delenv("REPOWIRE_RESTART_PEER_CAP_SEC", raising=False)
        assert _restart_peer_cap_sec() == 180.0

    def test_cap_env_override_still_applies(self, monkeypatch):
        from repowire.daemon.peer_registry import _restart_peer_cap_sec

        monkeypatch.setenv("REPOWIRE_RESTART_PEER_CAP_SEC", "240")
        assert _restart_peer_cap_sec() == 240.0
