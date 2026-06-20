"""beads-61w: hook-side auto-ACK emission after successful/failed injection.

The receiver's hook emits a synthetic notify back to the sender with a
distinct marker so the sender sees receipt within milliseconds — the
infra layer of the two-layer ACK protocol (see delegation-ack.md).
The receiver-LLM still authors the intent-ACK in its first response.
"""

from __future__ import annotations

from unittest.mock import patch

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


@pytest.fixture
def captured_ack_posts(monkeypatch):
    """Capture _daemon_post calls emitted by the auto-ACK path."""
    posted: list[dict] = []

    async def _fake_daemon_post(path, body):
        posted.append({"path": path, "body": body})

    monkeypatch.setattr(wh, "_daemon_post", _fake_daemon_post)
    monkeypatch.setattr(wh, "_resolve_my_name", lambda: "devops-head-claude-code")
    return posted


@pytest.mark.asyncio
async def test_notify_success_emits_auto_ack_queued(
    captured_send_keys, captured_ack_posts
):
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "[#notif-12345678] hi",
    }
    await handle_message(data, "%1")
    assert len(captured_ack_posts) == 1
    body = captured_ack_posts[0]["body"]
    assert body["from_peer"] == "devops-head-claude-code"
    assert body["to_peer"] == "director-claude-code"
    assert body["text"].startswith("[AUTO-ACK] notif-12345678 delivered: queued")
    assert "INFRA RECEIPT" in body["text"]
    assert body["bypass_circle"] is True


@pytest.mark.asyncio
async def test_notify_interrupt_emits_auto_ack_interrupted(
    captured_send_keys, captured_ack_posts
):
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "[#notif-23456789] urgent",
        "interrupt": True,
    }
    await handle_message(data, "%1")
    body = captured_ack_posts[0]["body"]
    assert body["text"].startswith("[AUTO-ACK] notif-23456789 delivered: interrupted")
    assert "INFRA RECEIPT" in body["text"]


@pytest.mark.asyncio
async def test_auto_ack_loop_prevention_skips_auto_ack_reply(
    captured_send_keys, captured_ack_posts
):
    """An [AUTO-ACK] reply must NOT generate another auto-ACK (ping-pong)."""
    data = {
        "type": "notify",
        "from_peer": "some-peer-claude-code",
        "text": "[AUTO-ACK] notif-99999999 delivered: queued",
    }
    await handle_message(data, "%1")
    assert captured_ack_posts == []


@pytest.mark.asyncio
async def test_auto_ack_loop_prevention_skips_auto_nack_reply(
    captured_send_keys, captured_ack_posts
):
    data = {
        "type": "notify",
        "from_peer": "some-peer-claude-code",
        "text": "[AUTO-NACK] notif-99999999 failed: pane unsafe",
    }
    await handle_message(data, "%1")
    assert captured_ack_posts == []


@pytest.mark.asyncio
async def test_auto_ack_service_peer_skip(captured_send_keys, captured_ack_posts):
    """Service peers (telegram, brain-admin) don't consume auto-ACK."""
    data = {
        "type": "notify",
        "from_peer": "telegram-claude-code",
        "from_peer_role": "service",
        "text": "[#notif-serviceA] from the user",
    }
    await handle_message(data, "%1")
    assert captured_ack_posts == []


@pytest.mark.asyncio
async def test_auto_ack_self_skip(
    captured_send_keys, captured_ack_posts, monkeypatch
):
    """from_peer == my_name → noop."""
    monkeypatch.setattr(wh, "_resolve_my_name", lambda: "devops-head-claude-code")
    data = {
        "type": "notify",
        "from_peer": "devops-head-claude-code",
        "text": "[#notif-selfselfs] self-note",
    }
    await handle_message(data, "%1")
    assert captured_ack_posts == []


@pytest.mark.asyncio
async def test_auto_ack_skipped_without_correlation_id(
    captured_send_keys, captured_ack_posts
):
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "naked message without correlation id",
    }
    await handle_message(data, "%1")
    assert captured_ack_posts == []


@pytest.mark.asyncio
async def test_auto_nack_on_send_keys_failure(monkeypatch, captured_ack_posts):
    monkeypatch.setattr(wh, "_tmux_send_keys", lambda pane, text, interrupt=False: False)
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "[#notif-fa11fa11] boom",
    }
    await handle_message(data, "%1")
    assert len(captured_ack_posts) == 1
    body = captured_ack_posts[0]["body"]
    assert body["text"].startswith("[AUTO-NACK] notif-fa11fa11 failed:")


@pytest.mark.asyncio
async def test_broadcast_success_emits_auto_ack(
    captured_send_keys, captured_ack_posts
):
    """Broadcasts with correlation id also get auto-ACK'd per sender."""
    data = {
        "type": "broadcast",
        "from_peer": "director-claude-code",
        "text": "[#notif-bca57001] ship-it",
    }
    await handle_message(data, "%1")
    assert len(captured_ack_posts) == 1
    body = captured_ack_posts[0]["body"]
    assert body["text"].startswith("[AUTO-ACK] notif-bca57001")


@pytest.mark.asyncio
async def test_query_success_emits_auto_ack(
    captured_send_keys, captured_ack_posts
):
    """Queries carry correlation_id in a dedicated WS field; auto-ACK scans the text
    because stop-hook already uses correlation_id for response delivery — but the
    text prefix lets a sender's queue infra see delivery without extra WS work.
    """
    data = {
        "type": "query",
        "correlation_id": "q-1",
        "from_peer": "director-claude-code",
        "text": "[#notif-aaaaaaab] request",
    }
    with patch.object(wh, "_push_pending_cid"):
        await handle_message(data, "%1")
    assert len(captured_ack_posts) == 1
    body = captured_ack_posts[0]["body"]
    assert body["text"].startswith("[AUTO-ACK] notif-aaaaaaab")


# ---------------------------------------------------------------------------
# beads-hqvm DoD6: AUTO-ACK reverse route targets the EXACT original sender by
# peer_id, so the receipt cannot misroute to a foreign-circle namesake.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_auto_ack_uses_from_peer_id_as_to_peer_id(
    captured_send_keys, captured_ack_posts
):
    """When the incoming notify carries from_peer_id, the AUTO-ACK replies to
    that exact peer_id (to_peer_id), not just the ambiguous display_name."""
    data = {
        "type": "notify",
        "from_peer": "backend-worker-claude-code",
        "from_peer_id": "repow-project-zeon-abcd1234",
        "text": "[#notif-deadbeef] hi",
    }
    await handle_message(data, "%1")
    body = captured_ack_posts[0]["body"]
    assert body["to_peer"] == "backend-worker-claude-code"
    assert body["to_peer_id"] == "repow-project-zeon-abcd1234"


@pytest.mark.asyncio
async def test_query_auto_ack_uses_from_peer_id_as_to_peer_id(
    captured_send_keys, captured_ack_posts
):
    data = {
        "type": "query",
        "correlation_id": "q-2",
        "from_peer": "backend-worker-claude-code",
        "from_peer_id": "repow-project-zeon-abcd1234",
        "text": "[#notif-cafef00d] req",
    }
    with patch.object(wh, "_push_pending_cid"):
        await handle_message(data, "%1")
    body = captured_ack_posts[0]["body"]
    assert body["to_peer_id"] == "repow-project-zeon-abcd1234"


@pytest.mark.asyncio
async def test_auto_nack_uses_from_peer_id_as_to_peer_id(
    monkeypatch, captured_ack_posts
):
    monkeypatch.setattr(wh, "_tmux_send_keys", lambda pane, text, interrupt=False: False)
    data = {
        "type": "notify",
        "from_peer": "backend-worker-claude-code",
        "from_peer_id": "repow-project-zeon-abcd1234",
        "text": "[#notif-0badf00d] boom",
    }
    await handle_message(data, "%1")
    body = captured_ack_posts[0]["body"]
    assert body["text"].startswith("[AUTO-NACK] notif-0badf00d")
    assert body["to_peer_id"] == "repow-project-zeon-abcd1234"


@pytest.mark.asyncio
async def test_auto_ack_omits_to_peer_id_when_absent(
    captured_send_keys, captured_ack_posts
):
    """Back-compat: no from_peer_id in the incoming message -> no to_peer_id key."""
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "[#notif-11112222] hi",
    }
    await handle_message(data, "%1")
    body = captured_ack_posts[0]["body"]
    assert "to_peer_id" not in body
