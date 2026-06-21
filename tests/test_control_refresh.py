"""Tests for POST /control/refresh-clients (beads-rz1g part 2).

Frozen contract (devops n8pt depends on this):
  body  {"target_epoch"?: str, "reason": str, "scope": "workers"|"all"|"advisory"}
  reply {"notified": <int>, "target_epoch": <str>}  200
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from repowire.config.models import Config
from repowire.daemon.deps import cleanup_deps, init_deps
from repowire.daemon.routes import control


def _make_app(*, refresh_epoch: str | None = "daemon-epoch-1", auth_token: str = ""):
    cfg = Config()
    if auth_token:
        cfg.daemon.auth_token = auth_token
    message_router = SimpleNamespace(broadcast_refresh=AsyncMock(return_value=["sid-1", "sid-2"]))
    app_state = SimpleNamespace(
        config=cfg,
        message_router=message_router,
        refresh_epoch=refresh_epoch,
        relay_mode=False,
    )
    # peer_registry is unused by control but init_deps requires a non-None value
    init_deps(cfg, SimpleNamespace(), app_state)
    app = FastAPI()
    app.include_router(control.router)
    return app, app_state, message_router


@pytest.fixture
async def ctx():
    app, app_state, message_router = _make_app()
    t = ASGITransport(app=app)
    async with AsyncClient(transport=t, base_url="http://test") as c:
        yield c, app_state, message_router
    cleanup_deps()


class TestRefreshClients:
    async def test_explicit_target_epoch_is_used_and_broadcast(self, ctx):
        client, app_state, mr = ctx
        r = await client.post(
            "/control/refresh-clients",
            json={"target_epoch": "0.10.0+999", "reason": "deploy", "scope": "workers"},
        )
        assert r.status_code == 200
        assert r.json() == {"notified": 2, "target_epoch": "0.10.0+999"}
        mr.broadcast_refresh.assert_awaited_once()
        kwargs = mr.broadcast_refresh.await_args.kwargs
        assert kwargs["target_epoch"] == "0.10.0+999"
        assert kwargs["reason"] == "deploy"
        assert kwargs["scope"] == "workers"
        # stored as the new authoritative epoch
        assert app_state.refresh_epoch == "0.10.0+999"

    async def test_omitted_target_epoch_defaults_to_daemon_epoch(self, ctx):
        """target_epoch optional → daemon substitutes its own deployed epoch."""
        client, app_state, mr = ctx
        r = await client.post(
            "/control/refresh-clients",
            json={"reason": "deploy", "scope": "all"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["target_epoch"] == "daemon-epoch-1"
        assert body["notified"] == 2
        assert mr.broadcast_refresh.await_args.kwargs["target_epoch"] == "daemon-epoch-1"

    async def test_scope_defaults_to_workers(self, ctx):
        client, _app_state, mr = ctx
        r = await client.post("/control/refresh-clients", json={"reason": "x"})
        assert r.status_code == 200
        assert mr.broadcast_refresh.await_args.kwargs["scope"] == "workers"

    async def test_invalid_scope_rejected(self, ctx):
        client, _app_state, _mr = ctx
        r = await client.post(
            "/control/refresh-clients",
            json={"reason": "x", "scope": "everybody"},
        )
        assert r.status_code == 422

    async def test_omitted_epoch_falls_back_to_computed_when_state_unset(self):
        """If the daemon never set refresh_epoch, it computes one on the fly."""
        app, _app_state, mr = _make_app(refresh_epoch=None)
        t = ASGITransport(app=app)
        async with AsyncClient(transport=t, base_url="http://test") as client:
            r = await client.post("/control/refresh-clients", json={"reason": "x"})
        cleanup_deps()
        assert r.status_code == 200
        # a real "<version>+<mtime>" epoch, not the empty/None sentinel
        assert "+" in r.json()["target_epoch"]


class TestRefreshAuth:
    async def test_auth_required_when_token_set(self):
        app, _app_state, _mr = _make_app(auth_token="secret")
        t = ASGITransport(app=app)
        async with AsyncClient(transport=t, base_url="http://test") as client:
            r = await client.post("/control/refresh-clients", json={"reason": "x"})
        cleanup_deps()
        assert r.status_code == 401

    async def test_auth_accepts_valid_token(self):
        app, _app_state, _mr = _make_app(auth_token="secret")
        t = ASGITransport(app=app)
        async with AsyncClient(transport=t, base_url="http://test") as client:
            r = await client.post(
                "/control/refresh-clients",
                json={"reason": "x"},
                headers={"Authorization": "Bearer secret"},
            )
        cleanup_deps()
        assert r.status_code == 200
