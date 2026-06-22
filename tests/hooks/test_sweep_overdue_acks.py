"""beads-nfap.2: shared overdue-ack sweep used by BOTH the ws-hook watchdog and
the stop-hook defense-in-depth sweep.

sweep_overdue_acks pops overdue pendings (atomic, exactly-once) and injects one
escalation each via the caller-supplied injector. No-op under the rollback flag.
Extracting it into utils keeps the escalation text/logic single-sourced so the
two call sites can never diverge or copy-paste.
"""

from __future__ import annotations

import pytest

import repowire.config.models as cfg_models
from repowire.hooks import utils


@pytest.fixture(autouse=True)
def _isolate_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg_models, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def _no_receipt_inline(monkeypatch):
    monkeypatch.delenv("REPOWIRE_RECEIPT_INLINE", raising=False)


PANE = "%12"


def test_sweep_injects_escalation_for_overdue():
    utils.register_pending_ack(PANE, "notif-11112222", deadline=100.0, to_peer="backend-head")
    injected: list[str] = []
    overdue = utils.sweep_overdue_acks(PANE, now=200.0, inject=injected.append)
    assert len(injected) == 1
    assert "notif-11112222" in injected[0]
    assert "backend-head" in injected[0]
    assert {e["correlation_id"] for e in overdue} == {"notif-11112222"}


def test_sweep_skips_within_deadline():
    utils.register_pending_ack(PANE, "notif-33334444", deadline=999.0, to_peer="pm")
    injected: list[str] = []
    overdue = utils.sweep_overdue_acks(PANE, now=200.0, inject=injected.append)
    assert injected == []
    assert overdue == []


def test_sweep_skips_resolved_pending():
    utils.register_pending_ack(PANE, "notif-55556666", deadline=100.0, to_peer="pm")
    utils.resolve_pending_ack(PANE, "notif-55556666", kind="ack", text="[AUTO-ACK] delivered")
    injected: list[str] = []
    utils.sweep_overdue_acks(PANE, now=200.0, inject=injected.append)
    assert injected == []


def test_sweep_pops_so_a_second_sweep_is_silent():
    """Exactly-once across the two sweepers: once one sweep pops an overdue entry,
    the next sweep (the other process) finds nothing — no double escalation."""
    utils.register_pending_ack(PANE, "notif-77778888", deadline=100.0, to_peer="pm")
    first: list[str] = []
    second: list[str] = []
    utils.sweep_overdue_acks(PANE, now=200.0, inject=first.append)
    utils.sweep_overdue_acks(PANE, now=300.0, inject=second.append)
    assert len(first) == 1
    assert second == []


def test_sweep_noop_under_inline_flag(monkeypatch):
    monkeypatch.setenv("REPOWIRE_RECEIPT_INLINE", "1")
    utils.register_pending_ack(PANE, "notif-9999aaaa", deadline=100.0, to_peer="pm")
    injected: list[str] = []
    overdue = utils.sweep_overdue_acks(PANE, now=200.0, inject=injected.append)
    assert injected == []
    assert overdue == []
    # the pending must survive — the flag means inline mode, so the entry is not popped
    state = utils.read_ack_state(PANE)
    assert "notif-9999aaaa" in state["pending"]


# --- beads-lfn6: liveness-aware grace-backoff (remainder of eg5x FIX #2) -------
#
# When an overdue pending's receiver is online but BUSY mid-turn longer than the
# 60s ACK deadline, the un-ACKed notify is NOT a delivery failure — the intent-ACK
# is merely late. Escalating it is a false-positive that provokes a redundant
# resend. With an injected liveness predicate the sweep grants a bounded grace
# extension to a live receiver instead of escalating.


@pytest.fixture
def _grace_policy(monkeypatch):
    """Deterministic grace knobs: +50s per round, max 3 rounds."""
    monkeypatch.setattr(utils, "_ACK_GRACE_BACKOFF_SEC", 50.0)
    monkeypatch.setattr(utils, "_ACK_MAX_GRACE_ROUNDS", 3)


def test_sweep_grace_backoff_when_receiver_live(_grace_policy):
    """Receiver online/busy → re-arm the deadline, do NOT escalate."""
    utils.register_pending_ack(PANE, "notif-aaaa0001", deadline=100.0, to_peer="director")
    injected: list[str] = []
    overdue = utils.sweep_overdue_acks(
        PANE, now=200.0, inject=injected.append, is_receiver_live=lambda _peer: True
    )
    assert injected == []
    assert overdue == []
    state = utils.read_ack_state(PANE)
    entry = state["pending"]["notif-aaaa0001"]
    assert entry["deadline"] == 250.0  # now (200) + backoff (50)
    assert entry["grace_count"] == 1


def test_sweep_escalates_when_receiver_offline(_grace_policy):
    """Receiver offline/unreachable → genuine delivery failure, escalate + pop."""
    utils.register_pending_ack(PANE, "notif-aaaa0002", deadline=100.0, to_peer="ghost-head")
    injected: list[str] = []
    overdue = utils.sweep_overdue_acks(
        PANE, now=200.0, inject=injected.append, is_receiver_live=lambda _peer: False
    )
    assert len(injected) == 1
    assert "notif-aaaa0002" in injected[0]
    # offline → the delivery-failure framing ("проверь получателя ... повтори")
    assert "Доставка могла не пройти" in injected[0]
    assert {e["correlation_id"] for e in overdue} == {"notif-aaaa0002"}
    assert {e["reason"] for e in overdue} == {"failed"}
    assert "notif-aaaa0002" not in utils.read_ack_state(PANE)["pending"]


def test_sweep_escalates_after_grace_rounds_exhausted(_grace_policy):
    """A still-online receiver that never confirms is surfaced once grace is spent —
    masking a broken receipt path forever is forbidden — but with the non-alarming
    'stalled' wording, NOT the delivery-failure text (else churn returns)."""
    utils.register_pending_ack(PANE, "notif-aaaa0003", deadline=100.0, to_peer="director")
    injected: list[str] = []
    now = 200.0
    # 3 grace rounds re-arm silently; the 4th (grace exhausted) escalates.
    for _ in range(utils._ACK_MAX_GRACE_ROUNDS):
        utils.sweep_overdue_acks(
            PANE, now=now, inject=injected.append, is_receiver_live=lambda _peer: True
        )
        assert injected == []
        now = utils.read_ack_state(PANE)["pending"]["notif-aaaa0003"]["deadline"]
    overdue = utils.sweep_overdue_acks(
        PANE, now=now, inject=injected.append, is_receiver_live=lambda _peer: True
    )
    assert len(injected) == 1
    assert "notif-aaaa0003" in injected[0]
    # exhausted-while-online → "delivered but slow", NOT "delivery may have failed"
    assert "доставлен" in injected[0]
    assert "Доставка могла не пройти" not in injected[0]
    assert {e["reason"] for e in overdue} == {"stalled"}
    assert "notif-aaaa0003" not in utils.read_ack_state(PANE)["pending"]


def test_sweep_grace_then_resolved_is_silent(_grace_policy):
    """Re-armed pending that gets its receipt before the extended deadline never escalates."""
    utils.register_pending_ack(PANE, "notif-aaaa0004", deadline=100.0, to_peer="director")
    injected: list[str] = []
    utils.sweep_overdue_acks(
        PANE, now=200.0, inject=injected.append, is_receiver_live=lambda _peer: True
    )
    assert injected == []
    # intent-ACK finally lands (post-deadline but receiver was busy, not failed)
    utils.resolve_pending_ack(PANE, "notif-aaaa0004", kind="intent", text="ACK notif-aaaa0004")
    utils.sweep_overdue_acks(
        PANE, now=999.0, inject=injected.append, is_receiver_live=lambda _peer: True
    )
    assert injected == []
    assert "notif-aaaa0004" not in utils.read_ack_state(PANE)["pending"]


def test_sweep_without_liveness_predicate_keeps_legacy_escalation():
    """Back-compat: no liveness source → escalate every overdue (pre-lfn6 behavior)."""
    utils.register_pending_ack(PANE, "notif-aaaa0005", deadline=100.0, to_peer="pm")
    injected: list[str] = []
    overdue = utils.sweep_overdue_acks(PANE, now=200.0, inject=injected.append)
    assert len(injected) == 1
    assert {e["correlation_id"] for e in overdue} == {"notif-aaaa0005"}
