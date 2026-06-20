"""Tests for the stop hook handler."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import repowire.config.models as cfg_models
from repowire.hooks import utils
from repowire.hooks.stop_handler import main


def _make_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a fake transcript JSONL file."""
    tp = tmp_path / "transcript.jsonl"
    with open(tp, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return tp


def _run_hook(input_data: dict) -> int:
    """Run the stop hook with given input data."""
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.read.return_value = json.dumps(input_data)
        return main()


class TestStopHandler:
    def test_returns_zero_on_invalid_json(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "not json"
            assert main() == 0

    def test_returns_zero_when_stop_hook_active(self):
        result = _run_hook({"stop_hook_active": True})
        assert result == 0

    def test_returns_zero_without_transcript(self):
        with patch("repowire.hooks.stop_handler.get_pane_id", return_value=None), \
             patch("repowire.hooks.stop_handler.update_status", return_value=True):
            result = _run_hook({
                "cwd": "/tmp/test",
                "session_id": "abc12345-rest",
            })
            assert result == 0

    @patch("repowire.hooks.stop_handler.daemon_post")
    @patch("repowire.hooks.stop_handler.update_status", return_value=True)
    @patch("repowire.hooks.stop_handler.get_pane_id", return_value="%42")
    def test_posts_chat_turns(self, mock_pane, mock_status, mock_post, tmp_path):
        tp = _make_transcript(tmp_path, [
            {"type": "user", "message": {"content": "Fix the bug"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Fixed!"},
            ]}},
        ])
        _run_hook({
            "cwd": str(tmp_path),
            "session_id": "abc12345-rest",
            "transcript_path": str(tp),
        })

        # Should post user turn, assistant turn, and response
        calls = mock_post.call_args_list
        paths = [c[0][0] for c in calls]
        assert "/events/chat" in paths
        assert "/response" in paths

    @patch("repowire.hooks.stop_handler.daemon_post")
    @patch("repowire.hooks.stop_handler.update_status", return_value=True)
    @patch("repowire.hooks.stop_handler.get_pane_id", return_value="%42")
    @patch("repowire.hooks.stop_handler.get_display_name", return_value="myproject-claude-code")
    def test_uses_display_name_as_peer_name(
        self, mock_name, mock_pane, mock_status, mock_post, tmp_path,
    ):
        tp = _make_transcript(tmp_path, [
            {"type": "user", "message": {"content": "Hi"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Hello!"},
            ]}},
        ])
        _run_hook({
            "cwd": str(tmp_path),
            "session_id": "abc12345-rest-of-id",
            "transcript_path": str(tp),
        })

        # peer name should come from get_display_name (daemon-assigned)
        chat_calls = [c for c in mock_post.call_args_list if c[0][0] == "/events/chat"]
        assert len(chat_calls) >= 1
        payload = chat_calls[0][0][1]
        assert payload["peer"] == "myproject-claude-code"

    @patch("repowire.hooks.stop_handler.daemon_post")
    @patch("repowire.hooks.stop_handler.update_status", return_value=True)
    @patch("repowire.hooks.stop_handler.get_pane_id", return_value="%42")
    def test_includes_tool_calls(self, mock_pane, mock_status, mock_post, tmp_path):
        tp = _make_transcript(tmp_path, [
            {"type": "user", "message": {"content": "Run tests"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "passed"},
            ]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Tests passed!"},
            ]}},
        ])
        _run_hook({
            "cwd": str(tmp_path),
            "session_id": "abc12345-rest",
            "transcript_path": str(tp),
        })

        # Find assistant chat_turn
        chat_calls = [c for c in mock_post.call_args_list if c[0][0] == "/events/chat"]
        assistant_calls = [c for c in chat_calls if c[0][1].get("role") == "assistant"]
        assert len(assistant_calls) == 1
        payload = assistant_calls[0][0][1]
        assert payload["tool_calls"] is not None
        assert len(payload["tool_calls"]) == 1
        assert payload["tool_calls"][0]["name"] == "Bash"
        assert "pytest" in payload["tool_calls"][0]["input"]

    @patch("repowire.hooks.stop_handler.daemon_post")
    @patch("repowire.hooks.stop_handler.update_status", return_value=True)
    @patch("repowire.hooks.stop_handler.get_pane_id", return_value="%42")
    def test_chat_turn_includes_pane_id(self, mock_pane, mock_status, mock_post, tmp_path):
        """Chat turn payloads should include pane_id for server-side peer_id resolution."""
        tp = _make_transcript(tmp_path, [
            {"type": "user", "message": {"content": "Hi"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Hello!"},
            ]}},
        ])
        _run_hook({
            "cwd": str(tmp_path),
            "session_id": "abc12345-rest",
            "transcript_path": str(tp),
        })

        chat_calls = [c for c in mock_post.call_args_list if c[0][0] == "/events/chat"]
        assert len(chat_calls) >= 1
        for call in chat_calls:
            payload = call[0][1]
            assert payload["pane_id"] == "%42"

    @patch("repowire.hooks.stop_handler.daemon_post")
    @patch("repowire.hooks.stop_handler.update_status", return_value=True)
    @patch("repowire.hooks.stop_handler.get_pane_id", return_value="%42")
    @patch("repowire.hooks.stop_handler.get_display_name", return_value="test-gemini")
    def test_gemini_after_agent_with_final_response(
        self, mock_name, mock_pane, mock_status, mock_post,
    ):
        """Test Gemini's AfterAgent hook which provides final_response but no transcript_path."""
        _run_hook({
            "hook_event_name": "AfterAgent",
            "cwd": "/tmp/test",
            "session_id": "gemini123-rest",
            "final_response": "I am finished.",
        })

        # Should update status
        mock_status.assert_called_once_with("%42", "online", use_pane_id=True)

        # Should post assistant turn for dashboard
        chat_calls = [c for c in mock_post.call_args_list if c[0][0] == "/events/chat"]
        assert len(chat_calls) == 1
        payload = chat_calls[0][0][1]
        assert payload["peer"] == "test-gemini"
        assert payload["role"] == "assistant"
        assert payload["text"] == "I am finished."

        # Should post response for query resolution
        response_calls = [c for c in mock_post.call_args_list if c[0][0] == "/response"]
        assert len(response_calls) == 1
        payload = response_calls[0][0][1]
        assert payload["pane_id"] == "%42"
        assert payload["text"] == "I am finished."


PANE = "%42"


class TestStopHandlerAckSweep:
    """beads-nfap.2: the Stop hook duplicates the ws-hook ack-watchdog as
    defense-in-depth — at every turn boundary it sweeps the per-pane ack-state and
    escalates un-ACKed overdue notifies, in case the ws-hook process is down."""

    @pytest.fixture(autouse=True)
    def _isolate_cache_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg_models, "CACHE_DIR", tmp_path / "cache")

    @pytest.fixture(autouse=True)
    def _no_receipt_inline(self, monkeypatch):
        monkeypatch.delenv("REPOWIRE_RECEIPT_INLINE", raising=False)

    def _run(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = json.dumps(
                {"cwd": "/tmp/test", "session_id": "abc12345-rest"}
            )
            return main()

    def test_escalates_overdue_pending(self):
        utils.register_pending_ack(PANE, "notif-aaaabbbb", deadline=100.0, to_peer="backend-head")
        sent: list[str] = []
        with patch("repowire.hooks.stop_handler.daemon_post"), \
             patch("repowire.hooks.stop_handler.update_status", return_value=True), \
             patch("repowire.hooks.stop_handler.get_pane_id", return_value=PANE), \
             patch(
                 "repowire.hooks.stop_handler.tmux_send_keys",
                 side_effect=lambda pane, text, interrupt=False: sent.append(text) or True,
             ):
            self._run()
        assert len(sent) == 1
        assert "notif-aaaabbbb" in sent[0]
        assert "backend-head" in sent[0]

    def test_skips_pending_within_deadline(self):
        utils.register_pending_ack(PANE, "notif-ccccdddd", deadline=9_999_999_999.0, to_peer="pm")
        sent: list[str] = []
        with patch("repowire.hooks.stop_handler.daemon_post"), \
             patch("repowire.hooks.stop_handler.update_status", return_value=True), \
             patch("repowire.hooks.stop_handler.get_pane_id", return_value=PANE), \
             patch(
                 "repowire.hooks.stop_handler.tmux_send_keys",
                 side_effect=lambda pane, text, interrupt=False: sent.append(text) or True,
             ):
            self._run()
        assert sent == []

    def test_skips_resolved_pending(self):
        utils.register_pending_ack(PANE, "notif-eeeeffff", deadline=100.0, to_peer="pm")
        utils.resolve_pending_ack(PANE, "notif-eeeeffff", kind="ack", text="[AUTO-ACK] delivered")
        sent: list[str] = []
        with patch("repowire.hooks.stop_handler.daemon_post"), \
             patch("repowire.hooks.stop_handler.update_status", return_value=True), \
             patch("repowire.hooks.stop_handler.get_pane_id", return_value=PANE), \
             patch(
                 "repowire.hooks.stop_handler.tmux_send_keys",
                 side_effect=lambda pane, text, interrupt=False: sent.append(text) or True,
             ):
            self._run()
        assert sent == []

    def test_gated_by_inline_rollback_flag(self, monkeypatch):
        monkeypatch.setenv("REPOWIRE_RECEIPT_INLINE", "1")
        utils.register_pending_ack(PANE, "notif-00001111", deadline=100.0, to_peer="pm")
        sent: list[str] = []
        with patch("repowire.hooks.stop_handler.daemon_post"), \
             patch("repowire.hooks.stop_handler.update_status", return_value=True), \
             patch("repowire.hooks.stop_handler.get_pane_id", return_value=PANE), \
             patch(
                 "repowire.hooks.stop_handler.tmux_send_keys",
                 side_effect=lambda pane, text, interrupt=False: sent.append(text) or True,
             ):
            self._run()
        assert sent == []

    def test_still_delivers_response_while_sweeping(self, tmp_path):
        """Existing stop_handler logic (chat turns + /response) stays intact when a
        sweep also runs."""
        utils.register_pending_ack(PANE, "notif-22223333", deadline=100.0, to_peer="pm")
        tp = tmp_path / "t.jsonl"
        tp.write_text(
            json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
            + json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
            )
            + "\n"
        )
        with patch("repowire.hooks.stop_handler.daemon_post") as mock_post, \
             patch("repowire.hooks.stop_handler.update_status", return_value=True), \
             patch("repowire.hooks.stop_handler.get_pane_id", return_value=PANE), \
             patch(
                 "repowire.hooks.stop_handler.tmux_send_keys",
                 side_effect=lambda pane, text, interrupt=False: True,
             ):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.read.return_value = json.dumps({
                    "cwd": str(tmp_path),
                    "session_id": "abc12345-rest",
                    "transcript_path": str(tp),
                })
                main()
        paths = [c[0][0] for c in mock_post.call_args_list]
        assert "/events/chat" in paths
        assert "/response" in paths
