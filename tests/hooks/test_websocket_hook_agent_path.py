"""Regression test: websocket_hook uses resolve_agent_path (REPOWIRE_AGENT_PATH preferred)."""
from unittest.mock import patch

from repowire.hooks._identity import resolve_agent_path


def test_uses_env_var_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("REPOWIRE_AGENT_PATH", "/agents/my-role")
    with patch("os.getcwd", return_value=str(tmp_path / "worktrees" / "agent-12345")):
        result = resolve_agent_path()
    assert result == "/agents/my-role"


def test_falls_back_to_cwd_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("REPOWIRE_AGENT_PATH", raising=False)
    fake_cwd = str(tmp_path / "agents")
    with patch("os.getcwd", return_value=fake_cwd):
        result = resolve_agent_path()
    assert result == fake_cwd
