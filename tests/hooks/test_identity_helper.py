"""Tests for repowire.hooks._identity.resolve_agent_path."""
import json
import sys
from unittest.mock import patch, MagicMock

import pytest

from repowire.hooks._identity import resolve_agent_path


# --- Unit tests for resolve_agent_path ---

def test_prefers_env_var_over_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("REPOWIRE_AGENT_PATH", "/agents/my-role")
    result = resolve_agent_path(fallback_cwd=str(tmp_path / "worktrees" / "agent-xyz"))
    assert result == "/agents/my-role"


def test_uses_fallback_cwd_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("REPOWIRE_AGENT_PATH", raising=False)
    fallback = str(tmp_path / "agents" / "my-project")
    result = resolve_agent_path(fallback_cwd=fallback)
    assert result == fallback


def test_uses_getcwd_when_no_env_and_no_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("REPOWIRE_AGENT_PATH", raising=False)
    with patch("os.getcwd", return_value=str(tmp_path)):
        result = resolve_agent_path()
    assert result == str(tmp_path)


def test_env_var_beats_getcwd(monkeypatch, tmp_path):
    monkeypatch.setenv("REPOWIRE_AGENT_PATH", "/agents/stable-name")
    with patch("os.getcwd", return_value=str(tmp_path / "worktrees" / "cryptic-1234")):
        result = resolve_agent_path()
    assert result == "/agents/stable-name"


# --- Regression: session_handler uses identity_path for _register_peer_http ---

def test_session_handler_uses_agent_path_env_for_registration(monkeypatch, tmp_path):
    """When REPOWIRE_AGENT_PATH is set, _register_peer_http receives it, not the worktree cwd."""
    agents_dir = str(tmp_path / "agents" / "backend-head")
    worktree_cwd = str(tmp_path / "worktrees" / "devops-worker-1776620398")
    monkeypatch.setenv("REPOWIRE_AGENT_PATH", agents_dir)

    import repowire.hooks.session_handler as sh
    captured = {}

    def fake_daemon_post(url, payload, **kwargs):
        captured["payload"] = payload
        return {"peer_id": "p-abc", "display_name": "backend-head"}

    def fake_daemon_get(url, **kwargs):
        return {"peers": []}

    def fake_ws_popen(*args, **kwargs):
        m = MagicMock()
        m.pid = 99999
        return m

    monkeypatch.setattr(sh, "daemon_post", fake_daemon_post)
    monkeypatch.setattr(sh, "daemon_get", fake_daemon_get)
    monkeypatch.delenv("REPOWIRE_PEER_ROLE", raising=False)
    monkeypatch.delenv("REPOWIRE_DISPLAY_NAME", raising=False)
    monkeypatch.delenv("REPOWIRE_CIRCLE", raising=False)

    input_data = json.dumps({
        "hook_event_name": "SessionStart",
        "cwd": worktree_cwd,
        "session_id": "sess-test-001",
    })

    with (
        patch("repowire.hooks.session_handler.get_tmux_info", return_value={
            "pane_id": "%42", "session_name": "test-session",
        }),
        patch("repowire.hooks.session_handler.ws_hook_lock_path", return_value=tmp_path / "lock"),
        patch("repowire.hooks.session_handler.ws_hook_pid_path", return_value=tmp_path / "pid"),
        patch("repowire.hooks.session_handler.get_pane_file", return_value="pane-42"),
        patch("repowire.hooks.session_handler.pane_logs_dir", return_value=tmp_path),
        patch("repowire.hooks.session_handler.clear_pane_runtime_state"),
        patch("repowire.hooks.session_handler.read_pane_runtime_metadata", return_value={}),
        patch("repowire.hooks.session_handler.write_pane_runtime_metadata"),
        patch("subprocess.Popen", side_effect=fake_ws_popen),
        patch("sys.stdin", __class__=type(sys.stdin)),
    ):
        import io
        with patch("sys.stdin", io.StringIO(input_data)):
            sh.main()

    assert "payload" in captured, "_register_peer_http (daemon_post) was not called"
    assert captured["payload"]["path"] == agents_dir, (
        f"Expected agents_dir {agents_dir!r}, got {captured['payload']['path']!r}"
    )
