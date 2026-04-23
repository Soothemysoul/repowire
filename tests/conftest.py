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
