"""Tests for the MCP server — authenticated from_peer_id propagation (beads-hqvm).

The MCP server resolves its own identity via a pane->peer_id daemon lookup. It
must propagate that authenticated peer_id (from_peer_id) on notify/ask/broadcast
so the daemon can resolve the sender unambiguously even under display_name
collisions across circles.
"""

from __future__ import annotations

import pytest

from repowire.mcp import server as mcp_server

MY_NAME = "backend-worker-claude-code"
MY_PEER_ID = "repow-project-zeon-abcd1234"


@pytest.fixture
def daemon_calls(monkeypatch):
    """Patch daemon_request + identity resolution; record outbound calls."""
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_daemon_request(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/peers/by-pane/"):
            return {"display_name": MY_NAME, "peer_id": MY_PEER_ID}
        if path == "/notify":
            return {"ok": True}
        if path == "/query":
            return {"text": "response"}
        if path == "/broadcast":
            return {"sent_to": ["someone"]}
        if path.startswith("/peers/"):
            return {"display_name": MY_NAME, "peer_id": MY_PEER_ID}
        return {}

    # Reset cached module state so each test resolves fresh.
    monkeypatch.setattr(mcp_server, "_cached_peer_name", None)
    monkeypatch.setattr(mcp_server, "_cached_peer_id", None, raising=False)
    monkeypatch.setattr(mcp_server, "_registered", True)  # skip registration path
    monkeypatch.setattr(mcp_server, "daemon_request", fake_daemon_request)
    monkeypatch.setattr(mcp_server, "get_pane_id", lambda: "%42")
    return calls


def _body_for(calls, path):
    for method, p, body in calls:
        if p == path:
            return body
    raise AssertionError(f"no call to {path}; calls={[c[1] for c in calls]}")


async def test_notify_includes_from_peer_id(daemon_calls):
    mcp = mcp_server.create_mcp_server()
    await mcp.call_tool("notify_peer", {"peer_name": "backend-head", "message": "ACK"})
    body = _body_for(daemon_calls, "/notify")
    assert body["from_peer"] == MY_NAME
    assert body["from_peer_id"] == MY_PEER_ID


async def test_ask_includes_from_peer_id(daemon_calls):
    mcp = mcp_server.create_mcp_server()
    await mcp.call_tool("ask_peer", {"peer_name": "backend-head", "query": "status?"})
    body = _body_for(daemon_calls, "/query")
    assert body["from_peer"] == MY_NAME
    assert body["from_peer_id"] == MY_PEER_ID


async def test_broadcast_includes_from_peer_id(daemon_calls):
    mcp = mcp_server.create_mcp_server()
    await mcp.call_tool("broadcast", {"message": "heads up"})
    body = _body_for(daemon_calls, "/broadcast")
    assert body["from_peer"] == MY_NAME
    assert body["from_peer_id"] == MY_PEER_ID


async def test_notify_omits_from_peer_id_when_unknown(monkeypatch):
    """When identity cannot be resolved (no pane), from_peer_id is omitted, not None-stuffed."""
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_daemon_request(method, path, body=None):
        calls.append((method, path, body))
        if path == "/notify":
            return {"ok": True}
        # by-pane / by-name lookups fail -> identity falls back to folder name
        raise mcp_server.DaemonHTTPError(404, "not found")

    monkeypatch.setattr(mcp_server, "_cached_peer_name", None)
    monkeypatch.setattr(mcp_server, "_cached_peer_id", None, raising=False)
    monkeypatch.setattr(mcp_server, "_registered", True)
    monkeypatch.setattr(mcp_server, "daemon_request", fake_daemon_request)
    monkeypatch.setattr(mcp_server, "get_pane_id", lambda: None)
    monkeypatch.setattr(mcp_server, "get_display_name", lambda: "fallback-name")

    mcp = mcp_server.create_mcp_server()
    await mcp.call_tool("notify_peer", {"peer_name": "backend-head", "message": "ACK"})
    body = _body_for(calls, "/notify")
    assert body["from_peer"] == "fallback-name"
    assert "from_peer_id" not in body


# ---------------------------------------------------------------------------
# beads-fqus: _get_my_peer_id must not let a cwd-fallback name-cache permanently
# suppress the authenticated peer_id. If the pane->peer_id lookup raced/failed on
# the first identity resolution (name cached via fallback, peer_id still None),
# a later _get_my_peer_id() call must re-resolve the pane->peer_id instead of
# short-circuiting on the cached name and returning None forever.
# ---------------------------------------------------------------------------


async def test_get_my_peer_id_reresolves_when_name_cached_without_id(monkeypatch):
    # Name already cached (e.g. via the cwd fallback) but peer_id never resolved.
    monkeypatch.setattr(mcp_server, "_cached_peer_name", "fallback-name")
    monkeypatch.setattr(mcp_server, "_cached_peer_id", None, raising=False)

    async def fake_daemon_request(method, path, body=None):
        if path.startswith("/peers/by-pane/"):
            return {"display_name": "real-name", "peer_id": "repow-xyz-9999"}
        return {}

    monkeypatch.setattr(mcp_server, "daemon_request", fake_daemon_request)
    monkeypatch.setattr(mcp_server, "get_pane_id", lambda: "%42")

    assert await mcp_server._get_my_peer_id() == "repow-xyz-9999"


async def test_get_my_peer_id_returns_none_when_no_pane(monkeypatch):
    monkeypatch.setattr(mcp_server, "_cached_peer_name", "fallback-name")
    monkeypatch.setattr(mcp_server, "_cached_peer_id", None, raising=False)
    monkeypatch.setattr(mcp_server, "get_pane_id", lambda: None)

    assert await mcp_server._get_my_peer_id() is None


async def test_get_my_peer_id_uses_cached_id_without_requery(monkeypatch):
    monkeypatch.setattr(mcp_server, "_cached_peer_id", "repow-cached-0001", raising=False)

    def _boom():
        raise AssertionError("must not re-resolve when peer_id already cached")

    monkeypatch.setattr(mcp_server, "get_pane_id", _boom)

    assert await mcp_server._get_my_peer_id() == "repow-cached-0001"
