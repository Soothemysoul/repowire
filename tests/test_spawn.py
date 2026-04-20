"""Tests for spawn module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repowire.spawn import (
    SpawnConfig,
    SpawnResult,
    _get_or_create_session,
    _unique_window_name,
    attach_session,
    kill_peer,
    kill_peer_by_pane,
    spawn_peer,
)


class TestSpawnConfig:
    """Tests for SpawnConfig dataclass."""

    def test_display_name_from_path(self) -> None:
        """Test display_name derives from path."""
        config = SpawnConfig(path="/home/user/myproject", circle="dev", backend="claude-code")
        assert config.display_name == "myproject"

    def test_display_name_nested_path(self) -> None:
        """Test display_name from nested path."""
        config = SpawnConfig(path="/home/user/git/frontend", circle="dev", backend="claude-code")
        assert config.display_name == "frontend"

    def test_display_name_trailing_slash(self) -> None:
        """Test display_name handles trailing slash."""
        config = SpawnConfig(path="/home/user/myproject/", circle="dev", backend="claude-code")
        # Path.name strips trailing slash
        assert config.display_name == "myproject"

    def test_default_command_empty(self) -> None:
        """Test default command is empty string."""
        config = SpawnConfig(path="/tmp/test", circle="dev", backend="claude-code")
        assert config.command == ""

    def test_custom_command(self) -> None:
        """Test custom command is stored."""
        config = SpawnConfig(
            path="/tmp/test",
            circle="dev",
            backend="claude-code",
            command="claude --model opus",
        )
        assert config.command == "claude --model opus"


class TestSpawnResult:
    """Tests for SpawnResult dataclass."""

    def test_spawn_result_fields(self) -> None:
        """Test SpawnResult has expected fields."""
        result = SpawnResult(
            display_name="myapp",
            tmux_session="default:myapp",
        )
        assert result.display_name == "myapp"
        assert result.tmux_session == "default:myapp"


class TestUniqueWindowName:
    """Tests for _unique_window_name helper."""

    def test_unique_name_no_conflict(self) -> None:
        """Test returns base name when no conflict."""
        mock_session = MagicMock()
        mock_session.windows = []

        name = _unique_window_name(mock_session, "frontend")
        assert name == "frontend"

    def test_unique_name_with_conflict(self) -> None:
        """Test appends suffix when name exists."""
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "frontend"
        mock_session.windows = [mock_window]

        name = _unique_window_name(mock_session, "frontend")
        assert name == "frontend-2"

    def test_unique_name_multiple_conflicts(self) -> None:
        """Test finds next available suffix."""
        mock_session = MagicMock()
        mock_windows = [MagicMock(), MagicMock(), MagicMock()]
        mock_windows[0].name = "frontend"
        mock_windows[1].name = "frontend-2"
        mock_windows[2].name = "frontend-3"
        mock_session.windows = mock_windows

        name = _unique_window_name(mock_session, "frontend")
        assert name == "frontend-4"

    def test_unique_name_gap_in_sequence(self) -> None:
        """Test finds first available suffix when there's a gap."""
        mock_session = MagicMock()
        mock_windows = [MagicMock(), MagicMock()]
        mock_windows[0].name = "frontend"
        mock_windows[1].name = "frontend-3"  # Gap at -2
        mock_session.windows = mock_windows

        name = _unique_window_name(mock_session, "frontend")
        assert name == "frontend-2"

    def test_unique_name_with_none_window_names(self) -> None:
        """Test handles windows with None names."""
        mock_session = MagicMock()
        mock_windows = [MagicMock(), MagicMock()]
        mock_windows[0].name = None  # Window without name
        mock_windows[1].name = "frontend"
        mock_session.windows = mock_windows

        name = _unique_window_name(mock_session, "frontend")
        assert name == "frontend-2"


class TestGetOrCreateSession:
    """Tests for _get_or_create_session helper."""

    @patch("repowire.spawn.libtmux.Server")
    def test_get_existing_session(self, mock_server_class: MagicMock) -> None:
        """Test returns existing session."""
        mock_server = MagicMock()
        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session

        result = _get_or_create_session(mock_server, "dev")

        assert result == mock_session
        mock_server.sessions.get.assert_called_once_with(session_name="dev")
        mock_server.new_session.assert_not_called()

    @patch("repowire.spawn.libtmux.Server")
    def test_create_new_session_when_not_exists(self, mock_server_class: MagicMock) -> None:
        """Test creates new session when not found."""
        mock_server = MagicMock()
        mock_server.sessions.get.return_value = None
        mock_new_session = MagicMock()
        mock_server.new_session.return_value = mock_new_session

        result = _get_or_create_session(mock_server, "dev")

        assert result == mock_new_session
        mock_server.new_session.assert_called_once_with(session_name="dev")

    @patch("repowire.spawn.libtmux.Server")
    def test_create_new_session_on_exception(self, mock_server_class: MagicMock) -> None:
        """Test creates new session when get raises exception."""
        from libtmux.exc import LibTmuxException

        mock_server = MagicMock()
        mock_server.sessions.get.side_effect = LibTmuxException("not found")
        mock_new_session = MagicMock()
        mock_server.new_session.return_value = mock_new_session

        result = _get_or_create_session(mock_server, "dev")

        assert result == mock_new_session
        mock_server.new_session.assert_called_once_with(session_name="dev")


class TestSpawnPeer:
    """Tests for spawn_peer function."""

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_peer_creates_tmux_window(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """Test spawn_peer creates a tmux window."""
        mock_session = MagicMock()
        mock_session.windows = []
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.id = "%42"
        mock_window.active_pane = mock_pane
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        config = SpawnConfig(path="/tmp/test", circle="dev", backend="claude-code")
        result = spawn_peer(config)

        assert result.display_name == "test-claude-code"
        assert result.tmux_session == "dev:test"
        mock_pane.send_keys.assert_called_once_with("claude", enter=True)

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_peer_uses_custom_command(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """Test spawn_peer uses custom command when provided."""
        mock_session = MagicMock()
        mock_session.windows = []
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.id = "%42"
        mock_window.active_pane = mock_pane
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        config = SpawnConfig(
            path="/tmp/test",
            circle="dev",
            backend="claude-code",
            command="claude --model opus",
        )
        spawn_peer(config)

        mock_pane.send_keys.assert_called_once_with("claude --model opus", enter=True)

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_peer_opencode_backend(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """Test spawn_peer uses opencode command for opencode backend."""
        mock_session = MagicMock()
        mock_session.windows = []
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.id = "%42"
        mock_window.active_pane = mock_pane
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        config = SpawnConfig(path="/tmp/test", circle="dev", backend="opencode")
        spawn_peer(config)

        mock_pane.send_keys.assert_called_once_with("opencode", enter=True)

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_peer_codex_backend(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """Test spawn_peer uses codex command for codex backend."""
        mock_session = MagicMock()
        mock_session.windows = []
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.id = "%42"
        mock_window.active_pane = mock_pane
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        from repowire.config.models import AgentType
        config = SpawnConfig(path="/tmp/test", circle="dev", backend=AgentType.CODEX)
        spawn_peer(config)

        mock_pane.send_keys.assert_called_once_with("codex", enter=True)

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_peer_unknown_backend_raises(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """Test spawn_peer raises for unknown backend."""
        mock_session = MagicMock()
        mock_session.windows = []
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.id = "%42"
        mock_window.active_pane = mock_pane
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        config = SpawnConfig(path="/tmp/test", circle="dev", backend="unknown")

        with pytest.raises(ValueError, match="Unknown agent type"):
            spawn_peer(config)

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_peer_no_active_pane_raises(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """Test spawn_peer raises when no active pane."""
        mock_session = MagicMock()
        mock_session.windows = []
        mock_window = MagicMock()
        mock_window.active_pane = None
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        config = SpawnConfig(path="/tmp/test", circle="dev", backend="claude-code")

        with pytest.raises(RuntimeError, match="Failed to get active pane"):
            spawn_peer(config)

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_peer_unique_window_name(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """Test spawn_peer handles duplicate window names."""
        mock_session = MagicMock()
        mock_existing_window = MagicMock()
        mock_existing_window.name = "test"
        mock_session.windows = [mock_existing_window]
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.id = "%42"
        mock_window.active_pane = mock_pane
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        config = SpawnConfig(path="/tmp/test", circle="dev", backend="claude-code")
        result = spawn_peer(config)

        assert result.display_name == "test-claude-code"
        assert result.tmux_session == "dev:test-2"


class TestKillPeer:
    """Tests for kill_peer function."""

    def test_kill_peer_invalid_session_format(self) -> None:
        """Test returns False for invalid session format."""
        result = kill_peer("no-colon-here")
        assert result is False

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_session_not_found(self, mock_server_class: MagicMock) -> None:
        """Test returns False when session doesn't exist."""
        mock_server = mock_server_class.return_value
        mock_server.sessions.get.return_value = None

        result = kill_peer("dev:frontend")
        assert result is False

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_window_not_found(self, mock_server_class: MagicMock) -> None:
        """Test returns False when window doesn't exist."""
        mock_server = mock_server_class.return_value
        mock_session = MagicMock()
        mock_session.windows.get.return_value = None
        mock_server.sessions.get.return_value = mock_session

        result = kill_peer("dev:frontend")
        assert result is False

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_success(self, mock_server_class: MagicMock) -> None:
        """Test returns True when window is killed."""
        mock_server = mock_server_class.return_value
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        result = kill_peer("dev:frontend")

        assert result is True
        mock_window.kill.assert_called_once()

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_exception_returns_false(self, mock_server_class: MagicMock) -> None:
        """Test returns False when libtmux raises exception."""
        from libtmux.exc import LibTmuxException

        mock_server = mock_server_class.return_value
        mock_server.sessions.get.side_effect = LibTmuxException("error")

        result = kill_peer("dev:frontend")
        assert result is False


class TestKillPeerByPane:
    """Tests for kill_peer_by_pane — stable pane-ID-based kill."""

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_by_pane_success(self, mock_server_class: MagicMock) -> None:
        """kill_peer_by_pane returns True and kills the window when pane is found."""
        mock_server = mock_server_class.return_value
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.pane_id = "%42"
        mock_window.panes = [mock_pane]
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        mock_server.sessions = [mock_session]

        result = kill_peer_by_pane("%42")

        assert result is True
        mock_window.kill.assert_called_once()

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_by_pane_not_found(self, mock_server_class: MagicMock) -> None:
        """kill_peer_by_pane returns False when pane_id is not in any session."""
        mock_server = mock_server_class.return_value
        mock_pane = MagicMock()
        mock_pane.pane_id = "%99"
        mock_window = MagicMock()
        mock_window.panes = [mock_pane]
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        mock_server.sessions = [mock_session]

        result = kill_peer_by_pane("%42")

        assert result is False

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_by_pane_exception_returns_false(self, mock_server_class: MagicMock) -> None:
        """kill_peer_by_pane returns False when libtmux raises."""
        from libtmux.exc import LibTmuxException

        mock_server = mock_server_class.return_value
        mock_server.sessions = MagicMock(side_effect=LibTmuxException("boom"))

        result = kill_peer_by_pane("%42")

        assert result is False

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_by_pane_renamed_window(self, mock_server_class: MagicMock) -> None:
        """kill_peer_by_pane finds the window even when its name was changed to an emoji."""
        mock_server = mock_server_class.return_value
        mock_window = MagicMock()
        mock_window.window_name = "📦"  # renamed by spawn-claude.sh
        mock_pane = MagicMock()
        mock_pane.pane_id = "%7"
        mock_window.panes = [mock_pane]
        mock_session = MagicMock()
        mock_session.windows = [mock_window]
        mock_server.sessions = [mock_session]

        result = kill_peer_by_pane("%7")

        assert result is True
        mock_window.kill.assert_called_once()

    @patch("repowire.spawn.libtmux.Server")
    def test_kill_peer_backcompat_tmux_session(self, mock_server_class: MagicMock) -> None:
        """Legacy kill_peer(tmux_session) still works unchanged (back-compat)."""
        mock_server = mock_server_class.return_value
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_session.windows.get.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session

        result = kill_peer("dev:frontend")

        assert result is True
        mock_window.kill.assert_called_once()


class TestAttachSession:
    """Tests for attach_session function."""

    @patch("repowire.spawn.subprocess.run")
    def test_attach_session_with_window(self, mock_run: MagicMock) -> None:
        """Test attach_session with session:window format."""
        attach_session("dev:frontend")

        assert mock_run.call_count == 2
        mock_run.assert_any_call(["tmux", "select-window", "-t", "dev:frontend"], check=False)
        mock_run.assert_any_call(["tmux", "attach-session", "-t", "dev"], check=True)

    @patch("repowire.spawn.subprocess.run")
    def test_attach_session_without_window(self, mock_run: MagicMock) -> None:
        """Test attach_session with session only."""
        attach_session("dev")

        assert mock_run.call_count == 2
        mock_run.assert_any_call(["tmux", "select-window", "-t", "dev"], check=False)
        mock_run.assert_any_call(["tmux", "attach-session", "-t", "dev"], check=True)


class TestMcpToolDescriptions:
    """Tests for MCP tool descriptions containing disambiguation markers."""

    def test_mcp_tools_have_mesh_prefix(self) -> None:
        """All repowire MCP tools should include [Repowire mesh] in their description."""
        from repowire.mcp.server import create_mcp_server
        mcp = create_mcp_server()
        mesh_tools = ["list_peers", "ask_peer", "notify_peer", "broadcast",
                       "spawn_peer", "kill_peer", "whoami", "set_description"]
        for name in mesh_tools:
            tool = mcp._tool_manager._tools.get(name)
            assert tool is not None, f"Tool {name} not found"
            desc = tool.description or ""
            assert "[Repowire mesh]" in desc, (
                f"Tool {name} missing [Repowire mesh] prefix in description"
            )

    def test_addressing_tools_warn_about_sendmessage(self) -> None:
        """Tools that send messages should warn against using SendMessage."""
        from repowire.mcp.server import create_mcp_server
        mcp = create_mcp_server()
        for name in ["ask_peer", "notify_peer", "broadcast", "spawn_peer"]:
            tool = mcp._tool_manager._tools.get(name)
            desc = tool.description or ""
            assert "SendMessage" in desc, (
                f"Tool {name} should mention SendMessage to prevent confusion"
            )


class TestMcpSpawnPeerReturn:
    """Tests for spawn_peer MCP tool return value."""

    @pytest.mark.asyncio
    @patch("repowire.mcp.server.daemon_request", new_callable=AsyncMock)
    async def test_spawn_peer_returns_display_name_and_tmux_session(
        self, mock_request: AsyncMock,
    ) -> None:
        """spawn_peer MCP tool should return both display_name and tmux_session."""
        mock_request.return_value = {
            "ok": True,
            "display_name": "alpha-svc",
            "tmux_session": "prod:alpha-svc",
        }

        from repowire.mcp.server import create_mcp_server
        mcp = create_mcp_server()
        tools = {name: fn for name, fn in mcp._tool_manager._tools.items()}
        spawn_tool = tools["spawn_peer"]
        result = await spawn_tool.fn(
            path="/tmp/alpha-svc", command="claude", circle="prod",
        )

        # Must mention both display_name and tmux_session distinctly
        assert "alpha-svc" in result
        assert "prod:alpha-svc" in result
        # Must NOT be just the raw tmux_session string
        assert result != "prod:alpha-svc"


class TestMcpRegistration:
    """Tests for MCP lazy registration behavior."""

    @pytest.mark.asyncio
    @patch("repowire.mcp.server.daemon_request", new_callable=AsyncMock)
    @patch(
        "repowire.mcp.server.get_tmux_info",
        return_value={"pane_id": "%1", "session_name": "0", "window_name": "repowire"},
    )
    async def test_tmux_lazy_registration_uses_pane_and_circle(
        self, _mock_tmux, mock_request: AsyncMock,
    ) -> None:
        """Tmux-backed MCP registration should converge on the pane-owned circle."""
        import repowire.mcp.server as mcp_server

        mcp_server._registered = False
        mcp_server._cached_peer_name = None
        mock_request.side_effect = [
            Exception("not found"),
            {"display_name": "repowire-codex"},
        ]

        import os as _os
        env_override = {"PATH": "/tmp/.codex/bin"}
        with patch.dict("os.environ", env_override, clear=False):
            _os.environ.pop("REPOWIRE_AGENT_PATH", None)
            await mcp_server._ensure_registered()

        assert mock_request.await_count == 2
        assert mock_request.await_args_list[0].args == ("GET", "/peers/by-pane/%251")
        _expected_path = str(mcp_server.Path.cwd())
        assert mock_request.await_args_list[1].args == (
            "POST",
            "/peers",
            {
                "name": mcp_server.Path(_expected_path).name,
                "path": _expected_path,
                "circle": "0",
                "backend": "codex",
                "pane_id": "%1",
            },
        )
        assert mcp_server._cached_peer_name == "repowire-codex"

        mcp_server._registered = False
        mcp_server._cached_peer_name = None

    @pytest.mark.asyncio
    @patch("repowire.mcp.server.daemon_request", new_callable=AsyncMock)
    @patch(
        "repowire.mcp.server.get_tmux_info",
        return_value={"pane_id": "%1", "session_name": "0", "window_name": "repowire"},
    )
    async def test_existing_pane_peer_skips_registration(
        self, _mock_tmux, mock_request: AsyncMock,
    ) -> None:
        """If the pane already has a peer, MCP should not create a duplicate."""
        import repowire.mcp.server as mcp_server

        mcp_server._registered = False
        mcp_server._cached_peer_name = None
        mock_request.return_value = {"display_name": "repowire-codex"}

        await mcp_server._ensure_registered()

        assert mock_request.await_count == 1
        assert mock_request.await_args_list[0].args == ("GET", "/peers/by-pane/%251")
        assert mcp_server._cached_peer_name == "repowire-codex"

        mcp_server._registered = False
        mcp_server._cached_peer_name = None


class TestTmuxServer:
    """Tests for _tmux_server helper."""

    @patch("repowire.spawn.libtmux.Server")
    def test_tmux_server_no_env(self, mock_server_class: MagicMock) -> None:
        """Test _tmux_server returns bare Server when env var is not set."""
        import os
        from repowire.spawn import _tmux_server

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REPOWIRE_TMUX_SOCKET", None)
            _tmux_server()

        mock_server_class.assert_called_once_with()

    @patch("repowire.spawn.libtmux.Server")
    def test_tmux_server_with_env(self, mock_server_class: MagicMock) -> None:
        """Test _tmux_server passes socket_name when REPOWIRE_TMUX_SOCKET is set."""
        import os
        from repowire.spawn import _tmux_server

        with patch.dict(os.environ, {"REPOWIRE_TMUX_SOCKET": "voice"}):
            _tmux_server()

        mock_server_class.assert_called_once_with(socket_name="voice")


class TestNaming:
    """Tests for shared naming helpers (repowire.naming)."""

    def test_sanitize_folder_name_basic(self) -> None:
        from repowire.naming import sanitize_folder_name
        assert sanitize_folder_name("qa-worker") == "qa-worker"

    def test_sanitize_folder_name_spaces(self) -> None:
        from repowire.naming import sanitize_folder_name
        assert sanitize_folder_name("my project") == "my-project"

    def test_sanitize_folder_name_collapses_hyphens(self) -> None:
        from repowire.naming import sanitize_folder_name
        assert sanitize_folder_name("my--project") == "my-project"

    def test_sanitize_folder_name_strips_edges(self) -> None:
        from repowire.naming import sanitize_folder_name
        assert sanitize_folder_name("-project-") == "project"

    def test_sanitize_folder_name_empty_fallback(self) -> None:
        from repowire.naming import sanitize_folder_name
        assert sanitize_folder_name("!!!") == "peer"

    def test_build_base_display_name(self) -> None:
        from repowire.config.models import AgentType
        from repowire.naming import build_base_display_name
        assert build_base_display_name("/home/user/qa-worker", AgentType.CLAUDE_CODE) == "qa-worker-claude-code"

    def test_build_base_display_name_none_path(self) -> None:
        from repowire.config.models import AgentType
        from repowire.naming import build_base_display_name
        assert build_base_display_name(None, AgentType.CLAUDE_CODE) == "peer-claude-code"


class TestSpawnDisplayNameMatchesDaemon:
    """Regression tests for beads-lyz: spawn_peer display_name must match daemon-assigned name.

    Root cause: routes/spawn.py registered spawn waiter under the tmux window
    name (e.g. 'qa-worker') while websocket.py fired the event under the daemon
    display_name (e.g. 'qa-worker-claude-code'). Keys never matched → 408.
    """

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_peer_display_name_includes_backend_suffix(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """SpawnResult.display_name must include the backend suffix matching daemon format."""
        mock_session = MagicMock()
        mock_session.windows = []
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.id = "%1"
        mock_window.active_pane = mock_pane
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        from repowire.config.models import AgentType
        config = SpawnConfig(path="/home/user/qa-worker", circle="default", backend=AgentType.CLAUDE_CODE)
        result = spawn_peer(config)

        assert result.display_name == "qa-worker-claude-code", (
            "display_name must match daemon's _build_display_name output so that "
            "register_spawn_waiter and _fire_spawn_event use the same key"
        )

    @patch("repowire.spawn._get_or_create_session")
    @patch("repowire.spawn.libtmux.Server")
    def test_spawn_display_name_consistent_across_5_spawns(
        self,
        mock_server_class: MagicMock,
        mock_get_session: MagicMock,
    ) -> None:
        """5 consecutive spawns all produce consistent display_name (backend suffix present)."""
        from repowire.config.models import AgentType

        mock_session = MagicMock()
        mock_session.windows = []
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.id = "%1"
        mock_window.active_pane = mock_pane
        mock_session.new_window.return_value = mock_window
        mock_get_session.return_value = mock_session

        for _ in range(5):
            config = SpawnConfig(path="/home/user/qa-worker", circle="default", backend=AgentType.CLAUDE_CODE)
            result = spawn_peer(config)
            assert result.display_name == "qa-worker-claude-code"


class TestSpawnWaitForReadyNoFalseNegative:
    """Integration regression for beads-lyz: wait_for_ready must not return 408 when peer connects."""

    @pytest.mark.asyncio
    async def test_register_and_fire_same_key(self) -> None:
        """register_spawn_waiter and _fire_spawn_event must agree on the key."""
        import asyncio
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from repowire.config.models import Config
        from repowire.daemon.message_router import MessageRouter
        from repowire.daemon.peer_registry import PeerRegistry
        from repowire.daemon.query_tracker import QueryTracker
        from repowire.daemon.websocket_transport import WebSocketTransport

        cfg = Config()
        transport = WebSocketTransport()
        tracker = QueryTracker()
        router = MessageRouter(transport=transport, query_tracker=tracker)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            registry = PeerRegistry(
                config=cfg,
                message_router=router,
                query_tracker=tracker,
                transport=transport,
                persistence_path=Path(tmp) / "sessions.json",
            )

            # Simulate: spawn_peer returns display_name = "qa-worker-claude-code"
            expected_name = "qa-worker-claude-code"
            event = registry.register_spawn_waiter(expected_name)

            # Simulate: WebSocket handler fires with daemon-assigned name
            registry._fire_spawn_event(expected_name)

            assert event.is_set(), (
                "Event must be set — register_spawn_waiter and _fire_spawn_event "
                "now use the same key, so wait_for_ready should not 408"
            )

    @pytest.mark.asyncio
    async def test_wait_for_ready_returns_200_not_408(self, tmp_path) -> None:
        """wait_for_ready=True must complete with 200 when spawn event fires in time."""
        import asyncio
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch, AsyncMock

        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from repowire.config.models import AgentType, Config
        from repowire.daemon.deps import cleanup_deps, init_deps
        from repowire.daemon.message_router import MessageRouter
        from repowire.daemon.peer_registry import PeerRegistry
        from repowire.daemon.query_tracker import QueryTracker
        from repowire.daemon.routes import spawn as spawn_routes
        from repowire.daemon.websocket_transport import WebSocketTransport
        from repowire.spawn import SpawnConfig, SpawnResult

        cfg = Config.model_validate({
            "daemon": {
                "spawn": {
                    "allowed_commands": ["claude"],
                    "allowed_paths": [str(tmp_path)],
                }
            }
        })
        transport = WebSocketTransport()
        tracker = QueryTracker()
        router = MessageRouter(transport=transport, query_tracker=tracker)
        registry = PeerRegistry(
            config=cfg,
            message_router=router,
            query_tracker=tracker,
            transport=transport,
            persistence_path=tmp_path / "sessions.json",
        )
        registry._events_path = tmp_path / "events.json"

        app_state = SimpleNamespace(
            config=cfg, transport=transport, query_tracker=tracker,
            message_router=router, peer_registry=registry, relay_mode=False,
        )
        init_deps(cfg, registry, app_state)

        app = FastAPI()
        app.include_router(spawn_routes.router)

        # Create a real path so validation passes
        project_path = tmp_path / "qa-worker"
        project_path.mkdir()

        expected_display_name = "qa-worker-claude-code"

        async def fake_spawn(path: str, command: str) -> None:
            """Fire spawn event shortly after spawn_peer is called."""
            await asyncio.sleep(0.05)
            registry._fire_spawn_event(expected_display_name)

        mock_result = SpawnResult(
            display_name=expected_display_name,
            tmux_session=f"default:qa-worker",
        )

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                with patch("repowire.daemon.routes.spawn.spawn_peer", return_value=mock_result):
                    # Fire the event concurrently, simulating peer connecting
                    task = asyncio.create_task(fake_spawn(str(project_path), "claude"))
                    r = await client.post("/spawn", json={
                        "path": str(project_path),
                        "command": "claude",
                        "circle": "default",
                        "wait_for_ready": True,
                        "ready_timeout_ms": 5000,
                    })
                    await task

            assert r.status_code == 200, (
                f"Expected 200 but got {r.status_code}: {r.text}. "
                "This is the beads-lyz regression: wait_for_ready must not 408 "
                "when the peer connects in time."
            )
            assert r.json()["status"] == "online"
        finally:
            cleanup_deps()
