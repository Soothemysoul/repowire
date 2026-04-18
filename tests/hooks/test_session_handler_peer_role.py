"""Test that session_handler passes REPOWIRE_PEER_ROLE into register payload."""
from unittest.mock import MagicMock, patch
import pytest

from repowire.hooks import session_handler


def _stub_daemon_post(monkeypatch):
    """Stub daemon_post inside session_handler so we can capture the payload."""
    captured = {}

    def fake_daemon_post(path, payload, **kwargs):
        captured["path"] = path
        captured["payload"] = payload
        return {"peer_id": "p-test", "display_name": "test-display"}

    monkeypatch.setattr(session_handler, "daemon_post", fake_daemon_post)
    return captured


def test_register_payload_includes_role_when_env_set(monkeypatch, tmp_path):
    monkeypatch.setenv("REPOWIRE_PEER_ROLE", "orchestrator")
    captured = _stub_daemon_post(monkeypatch)
    session_handler._register_peer_http(
        path=str(tmp_path),
        circle="global",
        backend="claude-code",
        pane_id="test_pane",
        metadata={},
    )
    payload = captured.get("payload")
    assert payload is not None, "daemon_post not called or payload missing"
    assert payload.get("role") == "orchestrator"


def test_register_payload_omits_role_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("REPOWIRE_PEER_ROLE", raising=False)
    captured = _stub_daemon_post(monkeypatch)
    session_handler._register_peer_http(
        path=str(tmp_path),
        circle="global",
        backend="claude-code",
        pane_id="test_pane",
        metadata={},
    )
    payload = captured.get("payload")
    assert payload is not None, "daemon_post not called or payload missing"
    assert "role" not in payload or payload.get("role") in (None, "")
