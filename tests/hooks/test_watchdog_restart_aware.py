"""beads-k1b3 (q3v5 L2): restart-aware ACK-watchdog grace.

A RESTARTING receiver is live-but-mid-respawn: its intent-ACK is merely delayed
on a LONGER timescale than a busy turn. The sweep must grant it a SEPARATE
restart-grace (distinct counter + backoff) rather than the busy-cap, and never
escalate it as a delivery failure while it is restarting. A restart that drags
past the restart-cap is a stuck restart = genuine failure → escalate (so a
failed restart is never masked forever).

Builds on the lfn6 two-phase sweep; the legacy ``is_receiver_live`` bool path is
untouched (its tests stay green). The new behaviour rides a ``receiver_status``
probe that returns the daemon status string.
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


@pytest.fixture
def _restart_policy(monkeypatch):
    """Deterministic knobs: busy grace +50s ×2; restart grace +100s ×3."""
    monkeypatch.setattr(utils, "_ACK_GRACE_BACKOFF_SEC", 50.0)
    monkeypatch.setattr(utils, "_ACK_MAX_GRACE_ROUNDS", 2)
    monkeypatch.setattr(utils, "_RESTART_GRACE_BACKOFF_SEC", 100.0)
    monkeypatch.setattr(utils, "_RESTART_MAX_GRACE_ROUNDS", 3)


PANE = "%34"


# -- receiver_status probe --

@pytest.mark.parametrize("status", ["online", "busy", "restarting", "offline"])
def test_receiver_status_returns_daemon_status(monkeypatch, status):
    monkeypatch.setattr(utils, "daemon_get", lambda path, **kw: {"status": status})
    assert utils.receiver_status("pm-claude-code") == status


def test_receiver_status_none_when_missing(monkeypatch):
    monkeypatch.setattr(utils, "daemon_get", lambda path, **kw: None)
    assert utils.receiver_status("ghost") is None


def test_receiver_status_uses_tight_timeout(monkeypatch):
    seen = {}

    def _fake_get(path, *, timeout=None):
        seen["timeout"] = timeout
        return {"status": "restarting"}

    monkeypatch.setattr(utils, "daemon_get", _fake_get)
    utils.receiver_status("pm")
    assert seen["timeout"] == utils._ACK_LIVENESS_TIMEOUT_SEC


# -- restart-aware sweep --

def test_restarting_receiver_gets_restart_grace(_restart_policy):
    """RESTARTING → re-arm with the RESTART backoff (not the busy backoff) and
    bump a SEPARATE restart_grace_count; no escalation."""
    utils.register_pending_ack(PANE, "notif-rrrr0001", deadline=100.0, to_peer="pm")
    injected: list[str] = []
    overdue = utils.sweep_overdue_acks(
        PANE, now=200.0, inject=injected.append,
        receiver_status=lambda _peer: "restarting",
    )
    assert injected == []
    assert overdue == []
    entry = utils.read_ack_state(PANE)["pending"]["notif-rrrr0001"]
    assert entry["deadline"] == 300.0  # now (200) + restart backoff (100)
    assert entry["restart_grace_count"] == 1
    # busy counter untouched
    assert entry.get("grace_count", 0) == 0


def test_online_receiver_uses_busy_grace_via_status(_restart_policy):
    """online/busy through the status probe still uses the busy backoff/cap."""
    utils.register_pending_ack(PANE, "notif-rrrr0002", deadline=100.0, to_peer="director")
    injected: list[str] = []
    utils.sweep_overdue_acks(
        PANE, now=200.0, inject=injected.append,
        receiver_status=lambda _peer: "busy",
    )
    assert injected == []
    entry = utils.read_ack_state(PANE)["pending"]["notif-rrrr0002"]
    assert entry["deadline"] == 250.0  # busy backoff 50
    assert entry["grace_count"] == 1


def test_offline_receiver_escalates_failed_via_status(_restart_policy):
    utils.register_pending_ack(PANE, "notif-rrrr0003", deadline=100.0, to_peer="ghost")
    injected: list[str] = []
    overdue = utils.sweep_overdue_acks(
        PANE, now=200.0, inject=injected.append,
        receiver_status=lambda _peer: "offline",
    )
    assert len(injected) == 1
    assert "Доставка могла не пройти" in injected[0]
    assert {e["reason"] for e in overdue} == {"failed"}


def test_restart_grace_exhausted_escalates_stuck(_restart_policy):
    """Restart that never returns within the restart-cap → escalate ONCE with the
    stuck-restart wording (a failed restart is not masked forever)."""
    utils.register_pending_ack(PANE, "notif-rrrr0004", deadline=100.0, to_peer="pm")
    injected: list[str] = []
    now = 200.0
    for _ in range(utils._RESTART_MAX_GRACE_ROUNDS):
        utils.sweep_overdue_acks(
            PANE, now=now, inject=injected.append,
            receiver_status=lambda _peer: "restarting",
        )
        assert injected == []
        now = utils.read_ack_state(PANE)["pending"]["notif-rrrr0004"]["deadline"]
    overdue = utils.sweep_overdue_acks(
        PANE, now=now, inject=injected.append,
        receiver_status=lambda _peer: "restarting",
    )
    assert len(injected) == 1
    assert "рестарт" in injected[0].lower()
    assert {e["reason"] for e in overdue} == {"restart_stalled"}
    assert "notif-rrrr0004" not in utils.read_ack_state(PANE)["pending"]


def test_restart_then_resolved_is_silent(_restart_policy):
    """A held-during-restart notify whose ACK lands after respawn never escalates."""
    utils.register_pending_ack(PANE, "notif-rrrr0005", deadline=100.0, to_peer="pm")
    injected: list[str] = []
    utils.sweep_overdue_acks(
        PANE, now=200.0, inject=injected.append,
        receiver_status=lambda _peer: "restarting",
    )
    assert injected == []
    utils.resolve_pending_ack(PANE, "notif-rrrr0005", kind="intent", text="ACK notif-rrrr0005")
    utils.sweep_overdue_acks(
        PANE, now=9999.0, inject=injected.append,
        receiver_status=lambda _peer: "online",
    )
    assert injected == []
