"""Tests for the client-code epoch marker (beads-rz1g part 1)."""

from __future__ import annotations

import repowire
from repowire.client_epoch import compute_client_epoch


def test_epoch_is_deterministic_within_a_process():
    """Two calls against the same on-disk install return the same epoch.

    Idempotency depends on this: a refresh signal compares a cached loaded-epoch
    against a target-epoch, and equal installs must compare equal.
    """
    assert compute_client_epoch() == compute_client_epoch()


def test_epoch_contains_version_and_separator():
    """Epoch is '<version>+<build-marker>' so it is human-greppable in markers."""
    epoch = compute_client_epoch()
    assert "+" in epoch
    assert epoch.startswith(f"{repowire.__version__}+")


def test_epoch_is_nonempty_string():
    epoch = compute_client_epoch()
    assert isinstance(epoch, str)
    assert epoch
