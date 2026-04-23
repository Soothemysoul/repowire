"""beads-61w: handle_message plumbs `interrupt` WS field into _tmux_send_keys,
and emits hook-side auto-ACK / auto-NACK via daemon_post after injection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import repowire.hooks.websocket_hook as wh
from repowire.hooks.websocket_hook import handle_message


@pytest.fixture(autouse=True)
def _mark_pane_safe(monkeypatch):
    """Short-circuit _is_pane_safe — tests do not touch tmux."""
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)


@pytest.fixture
def captured_send_keys(monkeypatch):
    """Capture calls to _tmux_send_keys and short-circuit actual tmux."""
    calls: list[dict] = []

    def _fake(pane_id, text, interrupt=False):
        calls.append({"pane_id": pane_id, "text": text, "interrupt": interrupt})
        return True

    monkeypatch.setattr(wh, "_tmux_send_keys", _fake)
    return calls


@pytest.mark.asyncio
async def test_notify_without_interrupt_calls_send_keys_false(captured_send_keys):
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "[#notif-aaaaaaaa] regular",
    }
    await handle_message(data, "%1")
    assert captured_send_keys[0]["interrupt"] is False


@pytest.mark.asyncio
async def test_notify_with_interrupt_true_propagates(captured_send_keys):
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "[#notif-bbbbbbbb] urgent",
        "interrupt": True,
    }
    await handle_message(data, "%1")
    assert captured_send_keys[0]["interrupt"] is True


@pytest.mark.asyncio
async def test_query_with_interrupt_true_propagates(captured_send_keys):
    data = {
        "type": "query",
        "correlation_id": "q-1",
        "from_peer": "director-claude-code",
        "text": "[#notif-cccccccc] urgent?",
        "interrupt": True,
    }
    # Patch the pending cid sidecar so it doesn't touch disk.
    with patch.object(wh, "_push_pending_cid"):
        await handle_message(data, "%1")
    assert captured_send_keys[0]["interrupt"] is True


@pytest.mark.asyncio
async def test_broadcast_with_interrupt_true_propagates(captured_send_keys):
    data = {
        "type": "broadcast",
        "from_peer": "director-claude-code",
        "text": "[#notif-dddddddd] BROADCAST",
        "interrupt": True,
    }
    await handle_message(data, "%1")
    assert captured_send_keys[0]["interrupt"] is True


# Auto-ACK tests land with commit-3 — see tests/hooks/test_websocket_hook_auto_ack.py.
