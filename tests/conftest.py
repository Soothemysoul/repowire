"""Shared pytest configuration.

Autouse fixtures here guard every test from accidentally touching
production paths. Add a new isolation here when a new code path learns
to write to `~/` or another shared location.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_interrupts_jsonl(monkeypatch, tmp_path):
    """beads-61w: redirect interrupts.jsonl to a per-test tmp file.

    Without this, any test that exercises `messages.notify_peer` with a
    `MagicMock()` request (whose `.interrupt` attribute is truthy) would
    append to the real `~/ai-infra/ops/repowire/interrupts.jsonl`. Tests
    that want to assert on the log must still set
    `REPOWIRE_INTERRUPT_LOG` to a path of their own — the autouse
    default just guarantees *none* of the log writes escape the tmp dir.
    """
    monkeypatch.setenv(
        "REPOWIRE_INTERRUPT_LOG",
        str(tmp_path / "autouse-interrupts.jsonl"),
    )


@pytest.fixture(autouse=True)
def _isolate_daemon_target(monkeypatch):
    """q2ok: never let a test reach the *live* daemon (default 127.0.0.1:8377).

    Hook helpers (`_daemon_post`, the ws-hook `main()` connect) fall back to
    ``127.0.0.1:8377`` when ``REPOWIRE_DAEMON_HOST/PORT`` are unset — the
    production daemon. A test exercising ``handle_message``'s auto-ACK path
    without mocking ``_daemon_post`` (e.g. tests/hooks/test_websocket_hook_interrupt.py)
    would POST a synthetic AUTO-ACK to that production daemon, which then injects
    it into a *live* peer's tmux pane — observed in the q2ok incident as
    placeholder ``notif-aaaaaaaa`` AUTO-ACKs landing in director's pane.

    Point the default at an unroutable loopback port so any unmocked post/connect
    fails fast and locally (auto-ACK is best-effort and swallows the error).
    Tests that need a real local target still override these vars themselves.
    """
    monkeypatch.setenv("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("REPOWIRE_DAEMON_PORT", "1")
