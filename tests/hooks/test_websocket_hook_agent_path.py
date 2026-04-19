"""Regression test: _resolve_agent_path prefers REPOWIRE_AGENT_PATH over os.getcwd()."""
from unittest.mock import patch

from repowire.hooks.websocket_hook import _resolve_agent_path


def test_uses_env_var_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("REPOWIRE_AGENT_PATH", "/agents/my-role")
    with patch("os.getcwd", return_value=str(tmp_path / "worktrees" / "agent-12345")):
        result = _resolve_agent_path()
    assert result == "/agents/my-role"


def test_falls_back_to_cwd_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("REPOWIRE_AGENT_PATH", raising=False)
    fake_cwd = str(tmp_path / "agents")
    with patch("os.getcwd", return_value=fake_cwd):
        result = _resolve_agent_path()
    assert result == fake_cwd
