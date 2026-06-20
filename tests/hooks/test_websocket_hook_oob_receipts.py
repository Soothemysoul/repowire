"""beads-nfap.1: sender-side out-of-band receipt intercept.

The sender's hook swallows delivery receipts (AUTO-ACK / intent-ACK) into the
per-pane ack-state file instead of injecting them as conversation turns —
success becomes complete silence. AUTO-NACK stays actionable and IS injected.
REPOWIRE_RECEIPT_INLINE=1 rolls back to the old inline pane-injection path.
"""

from __future__ import annotations

import pytest

import repowire.config.models as cfg_models
import repowire.hooks.utils as utils
import repowire.hooks.websocket_hook as wh
from repowire.hooks.websocket_hook import handle_message


@pytest.fixture(autouse=True)
def _mark_pane_safe(monkeypatch):
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)


@pytest.fixture(autouse=True)
def _isolate_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg_models, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def _no_receipt_inline(monkeypatch):
    """Default tests run in out-of-band mode; the rollback test opts back in."""
    monkeypatch.delenv("REPOWIRE_RECEIPT_INLINE", raising=False)


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
    posted: list[dict] = []

    async def _fake_daemon_post(path, body):
        posted.append({"path": path, "body": body})

    monkeypatch.setattr(wh, "_daemon_post", _fake_daemon_post)
    monkeypatch.setattr(wh, "_resolve_my_name", lambda: "backend-worker-claude-code")
    return posted


PANE = "%5"


@pytest.mark.asyncio
async def test_auto_ack_is_swallowed_not_injected(captured_send_keys, captured_ack_posts):
    """Incoming AUTO-ACK at the sender → no pane injection, recorded to ack-state."""
    utils.register_pending_ack(PANE, "notif-12345678", deadline=1e12, to_peer="backend-head")
    data = {
        "type": "notify",
        "from_peer": "backend-head-claude-code",
        "text": "[AUTO-ACK] notif-12345678 delivered: queued\n— INFRA RECEIPT, DO NOT REPLY",
    }
    await handle_message(data, PANE)
    assert captured_send_keys == []  # complete silence
    state = utils.read_ack_state(PANE)
    assert "notif-12345678" not in state["pending"]
    assert state["receipts"]["notif-12345678"]["kind"] == "ack"
    # loop-prevention: swallowing must not emit a fresh AUTO-ACK back
    assert captured_ack_posts == []


@pytest.mark.asyncio
async def test_intent_ack_is_swallowed_resolves_inner_cid(captured_send_keys, captured_ack_posts):
    """`[#notif-NEW] ACK notif-ORIG …` → swallowed; the ORIGINAL cid is resolved."""
    utils.register_pending_ack(PANE, "notif-deadbeef", deadline=1e12, to_peer="pm")
    data = {
        "type": "notify",
        "from_peer": "pm-claude-code",
        "text": "[#notif-99887766] ACK notif-deadbeef task=beads-x taken, starting.",
    }
    await handle_message(data, PANE)
    assert captured_send_keys == []
    state = utils.read_ack_state(PANE)
    assert "notif-deadbeef" not in state["pending"]
    assert state["receipts"]["notif-deadbeef"]["kind"] == "intent"


@pytest.mark.asyncio
async def test_auto_nack_is_actionable_and_injected(captured_send_keys, captured_ack_posts):
    """AUTO-NACK is a genuine delivery failure → still reaches the sender's pane."""
    utils.register_pending_ack(PANE, "notif-0badf00d", deadline=1e12, to_peer="backend-head")
    data = {
        "type": "notify",
        "from_peer": "backend-head-claude-code",
        "text": "[AUTO-NACK] notif-0badf00d failed: pane unsafe\n— INFRA RECEIPT",
    }
    await handle_message(data, PANE)
    assert len(captured_send_keys) == 1
    assert "[AUTO-NACK]" in captured_send_keys[0]["text"]
    # recorded too, so the watchdog won't double-escalate the same cid
    state = utils.read_ack_state(PANE)
    assert "notif-0badf00d" not in state["pending"]
    assert state["receipts"]["notif-0badf00d"]["kind"] == "nack"


@pytest.mark.asyncio
async def test_rollback_flag_injects_auto_ack_inline(
    captured_send_keys, captured_ack_posts, monkeypatch
):
    """REPOWIRE_RECEIPT_INLINE=1 restores the old inline pane-injection behavior."""
    monkeypatch.setenv("REPOWIRE_RECEIPT_INLINE", "1")
    data = {
        "type": "notify",
        "from_peer": "backend-head-claude-code",
        "text": "[AUTO-ACK] notif-12341234 delivered: queued",
    }
    await handle_message(data, PANE)
    assert len(captured_send_keys) == 1
    assert "[AUTO-ACK]" in captured_send_keys[0]["text"]


@pytest.mark.asyncio
async def test_normal_notify_still_injected(captured_send_keys, captured_ack_posts):
    """Regression: a non-receipt notify is unaffected and still injects."""
    data = {
        "type": "notify",
        "from_peer": "director-claude-code",
        "text": "[#notif-aabbccdd] обычное сообщение",
    }
    await handle_message(data, PANE)
    assert len(captured_send_keys) == 1
    assert "обычное сообщение" in captured_send_keys[0]["text"]


@pytest.mark.asyncio
async def test_broadcast_receipt_is_swallowed(captured_send_keys, captured_ack_posts):
    """A receipt arriving via the broadcast path is swallowed too (DoD: broadcast)."""
    data = {
        "type": "broadcast",
        "from_peer": "backend-head-claude-code",
        "text": "[AUTO-ACK] notif-bbbbcccc delivered: queued",
    }
    await handle_message(data, PANE)
    assert captured_send_keys == []
    state = utils.read_ack_state(PANE)
    assert state["receipts"]["notif-bbbbcccc"]["kind"] == "ack"
