"""beads-lfn6: receiver liveness probe backing the watchdog grace-backoff.

``receiver_is_live`` asks the daemon for the receiver's status. online/busy →
live (grace, do not escalate). offline / not-found / daemon-unreachable → not
live (escalate — never mask a genuine delivery failure).
"""

from __future__ import annotations

import pytest

from repowire.hooks import utils


@pytest.mark.parametrize(
    "status,expected",
    [("online", True), ("busy", True), ("offline", False)],
)
def test_receiver_is_live_by_status(monkeypatch, status, expected):
    monkeypatch.setattr(utils, "daemon_get", lambda path, **kw: {"status": status})
    assert utils.receiver_is_live("director-claude-code") is expected


def test_receiver_is_live_false_when_peer_missing(monkeypatch):
    """Daemon 404 → daemon_get returns None → treat as not live (escalate)."""
    monkeypatch.setattr(utils, "daemon_get", lambda path, **kw: None)
    assert utils.receiver_is_live("ghost") is False


def test_receiver_is_live_false_when_status_absent(monkeypatch):
    monkeypatch.setattr(utils, "daemon_get", lambda path, **kw: {})
    assert utils.receiver_is_live("weird") is False


def test_receiver_is_live_url_encodes_peer_name(monkeypatch):
    seen: list[str] = []

    def _fake_get(path, **kw):
        seen.append(path)
        return {"status": "online"}

    monkeypatch.setattr(utils, "daemon_get", _fake_get)
    utils.receiver_is_live("weird/name with space")
    assert seen == ["/peers/weird%2Fname%20with%20space"]


def test_receiver_is_live_uses_tight_timeout(monkeypatch):
    """A hung daemon must not stall the watchdog tick — the probe passes an
    explicit short timeout to daemon_get."""
    seen: dict = {}

    def _fake_get(path, *, timeout=None):
        seen["timeout"] = timeout
        return {"status": "busy"}

    monkeypatch.setattr(utils, "daemon_get", _fake_get)
    utils.receiver_is_live("director-claude-code")
    assert seen["timeout"] == utils._ACK_LIVENESS_TIMEOUT_SEC
    assert seen["timeout"] <= 2.0
