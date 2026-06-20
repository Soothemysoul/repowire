"""beads-nfap.1: per-pane ack-state file (out-of-band ACK receipts).

The sender's hook records delivery receipts to a flock'd per-pane state file
instead of injecting them as conversation turns. A watchdog reads overdue
pendings from the same file to escalate genuine delivery failures.
"""

from __future__ import annotations

import pytest

import repowire.config.models as cfg_models
from repowire.hooks import utils


@pytest.fixture(autouse=True)
def _isolate_cache_dir(monkeypatch, tmp_path):
    """Redirect CACHE_DIR (default ~/.cache/repowire) into a per-test tmp dir.

    pane_logs_dir() imports CACHE_DIR lazily, so patching the module attribute
    is enough to keep ack-state files out of the real home cache.
    """
    monkeypatch.setattr(cfg_models, "CACHE_DIR", tmp_path / "cache")


PANE = "%7"


def test_register_pending_then_read_shows_entry():
    utils.register_pending_ack(PANE, "notif-aaaaaaaa", deadline=1000.0, to_peer="backend-head")
    state = utils.read_ack_state(PANE)
    assert state["pending"]["notif-aaaaaaaa"]["to_peer"] == "backend-head"
    assert state["pending"]["notif-aaaaaaaa"]["deadline"] == 1000.0


def test_resolve_pending_removes_from_pending_and_records_receipt():
    utils.register_pending_ack(PANE, "notif-bbbbbbbb", deadline=1000.0, to_peer="pm")
    found = utils.resolve_pending_ack(
        PANE, "notif-bbbbbbbb", kind="ack", text="[AUTO-ACK] notif-bbbbbbbb delivered: queued"
    )
    assert found is True
    state = utils.read_ack_state(PANE)
    assert "notif-bbbbbbbb" not in state["pending"]
    assert state["receipts"]["notif-bbbbbbbb"]["kind"] == "ack"
    assert "delivered" in state["receipts"]["notif-bbbbbbbb"]["text"]


def test_resolve_unknown_cid_still_records_receipt_returns_false():
    """A receipt may arrive for a cid we never registered (e.g. intent-ACK for a
    notify sent before this pane learned the watchdog). Record it, report not-found."""
    found = utils.resolve_pending_ack(
        PANE, "notif-cccccccc", kind="intent", text="ACK notif-cccccccc taken"
    )
    assert found is False
    state = utils.read_ack_state(PANE)
    assert state["receipts"]["notif-cccccccc"]["kind"] == "intent"


def test_pop_overdue_returns_and_removes_only_past_deadline():
    utils.register_pending_ack(PANE, "notif-old00000", deadline=100.0, to_peer="a")
    utils.register_pending_ack(PANE, "notif-future00", deadline=999.0, to_peer="b")
    overdue = utils.pop_overdue_acks(PANE, now=500.0)
    cids = {e["correlation_id"] for e in overdue}
    assert cids == {"notif-old00000"}
    assert overdue[0]["to_peer"] == "a"
    # popped ones are gone; future one survives
    state = utils.read_ack_state(PANE)
    assert "notif-old00000" not in state["pending"]
    assert "notif-future00" in state["pending"]


def test_pop_overdue_empty_when_no_state_file():
    assert utils.pop_overdue_acks("%nonexistent", now=999999.0) == []


def test_clear_ack_state_removes_file():
    utils.register_pending_ack(PANE, "notif-dddddddd", deadline=1.0, to_peer="x")
    assert utils.ack_state_path(PANE).exists()
    utils.clear_ack_state(PANE)
    assert not utils.ack_state_path(PANE).exists()


def test_clear_pane_runtime_state_also_clears_ack_state():
    utils.register_pending_ack(PANE, "notif-eeeeeeee", deadline=1.0, to_peer="x")
    assert utils.ack_state_path(PANE).exists()
    utils.clear_pane_runtime_state(PANE)
    assert not utils.ack_state_path(PANE).exists()
