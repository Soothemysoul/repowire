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
