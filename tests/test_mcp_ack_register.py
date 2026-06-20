"""beads-nfap.1: MCP notify_peer registers a pending ACK for the watchdog.

Each outgoing notify registers its correlation_id with a deadline in the sender's
per-pane ack-state file. The ws-hook watchdog escalates the ones that go un-ACKed.
Notifies to non-ACKing service peers (telegram/dashboard/slack) are NOT registered
— they never run the tmux AUTO-ACK hook, so a pending would falsely escalate; and
their delivery failures already surface synchronously via the MCP tool error.
"""

from __future__ import annotations

import pytest

from repowire.mcp import server as mcp_server

MY_NAME = "backend-worker-claude-code"
MY_PEER_ID = "repow-project-zeon-abcd1234"


@pytest.fixture
def registered(monkeypatch):
    """Patch daemon_request + identity; capture register_pending_ack calls."""
    pendings: list[dict] = []

    async def fake_daemon_request(method, path, body=None):
        if path.startswith("/peers/by-pane/"):
            return {"display_name": MY_NAME, "peer_id": MY_PEER_ID}
        if path == "/notify":
            return {"ok": True}
        if path == "/broadcast":
            return {"sent_to": []}
        return {}

    def fake_register(pane_id, correlation_id, *, deadline, to_peer):
        pendings.append(
            {"pane_id": pane_id, "cid": correlation_id, "deadline": deadline, "to_peer": to_peer}
        )

    monkeypatch.setattr(mcp_server, "_cached_peer_name", None)
    monkeypatch.setattr(mcp_server, "_cached_peer_id", None, raising=False)
    monkeypatch.setattr(mcp_server, "_registered", True)
    monkeypatch.setattr(mcp_server, "daemon_request", fake_daemon_request)
    monkeypatch.setattr(mcp_server, "get_pane_id", lambda: "%42")
    monkeypatch.setattr(mcp_server, "register_pending_ack", fake_register)
    monkeypatch.delenv("REPOWIRE_RECEIPT_INLINE", raising=False)
    return pendings


async def test_notify_to_agent_registers_pending(registered):
    mcp = mcp_server.create_mcp_server()
    await mcp.call_tool("notify_peer", {"peer_name": "backend-head", "message": "go"})
    assert len(registered) == 1
    entry = registered[0]
    assert entry["pane_id"] == "%42"
    assert entry["to_peer"] == "backend-head"
    assert entry["cid"].startswith("notif-")


async def test_notify_to_telegram_is_not_registered(registered):
    mcp = mcp_server.create_mcp_server()
    await mcp.call_tool("notify_peer", {"peer_name": "telegram", "message": "hi user"})
    assert registered == []


async def test_notify_to_dashboard_is_not_registered(registered):
    mcp = mcp_server.create_mcp_server()
    await mcp.call_tool("notify_peer", {"peer_name": "dashboard", "message": "hi"})
    assert registered == []


async def test_notify_not_registered_under_inline_rollback(registered, monkeypatch):
    monkeypatch.setenv("REPOWIRE_RECEIPT_INLINE", "1")
    mcp = mcp_server.create_mcp_server()
    await mcp.call_tool("notify_peer", {"peer_name": "backend-head", "message": "go"})
    assert registered == []
