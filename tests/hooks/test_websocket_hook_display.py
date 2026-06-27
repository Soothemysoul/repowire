"""beads-rbox D1/D2: pane injection applies the display transform.

Agent↔agent pane prefix: ``@<peer>: <text>`` becomes
``@<display_peer_name>: <display_text>`` — suffix stripped only for
bypasses-circles roles (so the name stays addressable), notif token shortened
to ``[notif-XXX]`` (``#`` dropped). The canonical wire-text reaching the
correlation/ACK path is unchanged — see test_rbox_correlation_invariant.py.
"""

from __future__ import annotations

import pytest

import repowire.hooks.websocket_hook as wh
from repowire.hooks.websocket_hook import handle_message


@pytest.fixture(autouse=True)
def _mark_pane_safe(monkeypatch):
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)


@pytest.fixture
def captured_send_keys(monkeypatch):
    calls: list[dict] = []

    def _fake(pane_id, text, interrupt=False):
        calls.append({"pane_id": pane_id, "text": text, "interrupt": interrupt})
        return True

    monkeypatch.setattr(wh, "_tmux_send_keys", _fake)
    return calls


@pytest.mark.asyncio
async def test_notify_orchestrator_strips_suffix_and_shortens_notif(captured_send_keys):
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "from_peer_role": "orchestrator",
        "text": "[#notif-deadbeef] do the thing",
    }
    await handle_message(data, "%1")
    assert captured_send_keys[0]["text"] == "@director: [notif-deadbeef] do the thing"


@pytest.mark.asyncio
async def test_notify_agent_keeps_full_name_but_shortens_notif(captured_send_keys):
    # Variant A: regular agent name must stay addressable → suffix kept.
    data = {
        "type": "notify",
        "from_peer": "backend-head-claude-code",
        "from_peer_role": "agent",
        "text": "[#notif-deadbeef] implement beads-x",
    }
    await handle_message(data, "%1")
    assert (
        captured_send_keys[0]["text"]
        == "@backend-head-claude-code: [notif-deadbeef] implement beads-x"
    )


@pytest.mark.asyncio
async def test_notify_unknown_role_keeps_full_name(captured_send_keys):
    data = {
        "type": "notify",
        "from_peer": "backend-head-claude-code",
        "text": "[#notif-deadbeef] msg",
    }
    await handle_message(data, "%1")
    assert (
        captured_send_keys[0]["text"]
        == "@backend-head-claude-code: [notif-deadbeef] msg"
    )


@pytest.mark.asyncio
async def test_broadcast_orchestrator_strips_suffix(captured_send_keys):
    # Broadcast carries no notif token; only the peer name is transformed.
    data = {
        "type": "broadcast",
        "from_peer": "director-claude-code",
        "from_peer_role": "orchestrator",
        "text": "standup in 5",
    }
    await handle_message(data, "%1")
    assert captured_send_keys[0]["text"] == "@director [broadcast]: standup in 5"


@pytest.mark.asyncio
async def test_broadcast_agent_keeps_full_name(captured_send_keys):
    data = {
        "type": "broadcast",
        "from_peer": "qa-head-claude-code",
        "from_peer_role": "agent",
        "text": "ci is green",
    }
    await handle_message(data, "%1")
    assert captured_send_keys[0]["text"] == "@qa-head-claude-code [broadcast]: ci is green"
