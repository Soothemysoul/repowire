"""Regression: _tmux_send_keys exits copy-mode before injecting text (beads-xak)."""
from unittest.mock import call, patch

from repowire.hooks.websocket_hook import _tmux_send_keys


def _make_run(returncode=0, stdout=""):
    """Return a mock subprocess.run result."""
    import subprocess

    result = subprocess.CompletedProcess(args=[], returncode=returncode)
    result.stdout = stdout
    return result


def _collect_cmds(mock_run):
    """Extract the list-of-args from each subprocess.run call."""
    return [c.args[0] for c in mock_run.call_args_list]


def test_exits_copy_mode_before_send_keys():
    """send-keys -X cancel must precede send-keys -l when pane is in copy-mode."""
    with patch("subprocess.run", return_value=_make_run()) as mock_run, \
         patch("time.sleep"):
        result = _tmux_send_keys("%1", "hello world")

    assert result is True
    cmds = _collect_cmds(mock_run)
    cancel_idx = next(i for i, c in enumerate(cmds) if "-X" in c and "cancel" in c)
    literal_idx = next(i for i, c in enumerate(cmds) if "-l" in c)
    assert cancel_idx < literal_idx, "cancel must come before -l inject"


def test_cancel_called_even_outside_copy_mode():
    """Unconditional cancel: must always be called regardless of pane mode."""
    with patch("subprocess.run", return_value=_make_run()) as mock_run, \
         patch("time.sleep"):
        _tmux_send_keys("%2", "some text")

    cmds = _collect_cmds(mock_run)
    cancel_calls = [c for c in cmds if "-X" in c and "cancel" in c]
    assert len(cancel_calls) == 1, "cancel must be called exactly once per inject"


def test_normal_mode_still_sends_text_and_enter():
    """Full sequence: cancel → -l text → Escape → Enter (normal pane)."""
    with patch("subprocess.run", return_value=_make_run()) as mock_run, \
         patch("time.sleep"):
        result = _tmux_send_keys("%3", "ping")

    assert result is True
    cmds = _collect_cmds(mock_run)
    assert any("-l" in c for c in cmds), "literal send-keys required"
    assert any("Enter" in c for c in cmds), "Enter required"
    assert any("Escape" in c for c in cmds), "Escape required"
