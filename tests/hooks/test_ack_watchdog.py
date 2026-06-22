"""beads-nfap.1: ws-hook watchdog for un-ACKed outgoing notifies.

When an outgoing notify is not confirmed (no AUTO-ACK / intent-ACK) before its
deadline, the persistent ws-hook injects exactly ONE escalation prompt — the
genuine actionable case. Success (a timely receipt that clears the pending) stays
completely silent.
"""

from __future__ import annotations

import pytest

import repowire.config.models as cfg_models
import repowire.hooks.utils as utils
import repowire.hooks.websocket_hook as wh


@pytest.fixture(autouse=True)
def _isolate_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg_models, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def _no_receipt_inline(monkeypatch):
    monkeypatch.delenv("REPOWIRE_RECEIPT_INLINE", raising=False)


@pytest.fixture(autouse=True)
def _receiver_offline(monkeypatch):
    """Default these legacy tests to an offline receiver (no daemon dependency).

    beads-lfn6 routed the watchdog through a liveness probe; without this patch
    the driver would hit the real daemon. Offline → escalate, matching every
    assertion below. Grace-backoff tests override this locally.
    """
    monkeypatch.setattr(wh, "receiver_is_live", lambda _peer: False)


@pytest.fixture
def captured_send_keys(monkeypatch):
    calls: list[dict] = []

    def _fake(pane_id, text, interrupt=False):
        calls.append({"pane_id": pane_id, "text": text, "interrupt": interrupt})
        return True

    monkeypatch.setattr(wh, "_tmux_send_keys", _fake)
    return calls


PANE = "%9"


def test_overdue_pending_triggers_one_escalation(captured_send_keys):
    utils.register_pending_ack(PANE, "notif-11112222", deadline=100.0, to_peer="backend-head")
    wh._run_ack_watchdog_once(PANE, now=200.0)
    assert len(captured_send_keys) == 1
    text = captured_send_keys[0]["text"]
    assert "notif-11112222" in text
    assert "backend-head" in text


def test_pending_within_deadline_is_not_escalated(captured_send_keys):
    utils.register_pending_ack(PANE, "notif-33334444", deadline=999.0, to_peer="pm")
    wh._run_ack_watchdog_once(PANE, now=200.0)
    assert captured_send_keys == []


def test_resolved_pending_is_not_escalated(captured_send_keys):
    utils.register_pending_ack(PANE, "notif-55556666", deadline=100.0, to_peer="pm")
    # a receipt arrives and clears it before the watchdog ticks
    utils.resolve_pending_ack(
        PANE, "notif-55556666", kind="ack", text="[AUTO-ACK] notif-55556666 delivered: queued"
    )
    wh._run_ack_watchdog_once(PANE, now=200.0)
    assert captured_send_keys == []


def test_escalation_fires_exactly_once_across_ticks(captured_send_keys):
    utils.register_pending_ack(PANE, "notif-77778888", deadline=100.0, to_peer="pm")
    wh._run_ack_watchdog_once(PANE, now=200.0)
    wh._run_ack_watchdog_once(PANE, now=300.0)
    assert len(captured_send_keys) == 1


def test_inline_rollback_disables_watchdog(captured_send_keys, monkeypatch):
    monkeypatch.setenv("REPOWIRE_RECEIPT_INLINE", "1")
    utils.register_pending_ack(PANE, "notif-9999aaaa", deadline=100.0, to_peer="pm")
    wh._run_ack_watchdog_once(PANE, now=200.0)
    assert captured_send_keys == []


def test_busy_receiver_gets_grace_not_escalation(captured_send_keys, monkeypatch):
    """beads-lfn6: a busy-but-online director must NOT trigger a false escalation —
    the driver re-arms the pending instead of injecting the failure prompt."""
    monkeypatch.setattr(wh, "receiver_is_live", lambda _peer: True)
    utils.register_pending_ack(PANE, "notif-bbbb0001", deadline=100.0, to_peer="director")
    wh._run_ack_watchdog_once(PANE, now=200.0)
    assert captured_send_keys == []
    assert "notif-bbbb0001" in utils.read_ack_state(PANE)["pending"]
