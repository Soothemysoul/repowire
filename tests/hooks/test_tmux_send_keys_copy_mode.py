"""Regression: _tmux_send_keys exits copy-mode before injecting text (beads-xak)."""
import subprocess
from unittest.mock import call, patch

from repowire.hooks.websocket_hook import _tmux_send_keys, _wait_for_normal_mode


def _make_run(returncode=0, stdout=""):
    """Return a mock subprocess.run result."""
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


def test_timing_race_waits_for_copy_mode_exit():
    """Regression beads-xak: tmux processes cancel async — poll pane_in_mode=0 before -l.

    Simulates pane still in copy-mode for 2 polls after cancel, then exits.
    Verifies that display-message is called between cancel and -l, and that
    -l is only issued after pane_in_mode reaches 0.
    """
    call_log: list[list[str]] = []
    pane_in_mode_calls = 0

    def _fake_run(cmd, **kwargs):
        call_log.append(cmd)
        if "display-message" in cmd and "#{pane_in_mode}" in cmd:
            nonlocal pane_in_mode_calls
            pane_in_mode_calls += 1
            # First 2 calls: still in copy-mode; 3rd call: exited
            stdout = "1" if pane_in_mode_calls <= 2 else "0"
            result = subprocess.CompletedProcess(args=cmd, returncode=0)
            result.stdout = stdout
            return result
        result = subprocess.CompletedProcess(args=cmd, returncode=0)
        result.stdout = ""
        return result

    with patch("subprocess.run", side_effect=_fake_run), \
         patch("time.sleep"):
        success = _tmux_send_keys("%4", "f:T/dangerous-text")

    assert success is True

    cancel_idx = next(i for i, c in enumerate(call_log) if "-X" in c and "cancel" in c)
    display_idxs = [i for i, c in enumerate(call_log) if "display-message" in c and "#{pane_in_mode}" in c]
    literal_idx = next(i for i, c in enumerate(call_log) if "-l" in c)

    assert display_idxs, "display-message must be called to poll pane_in_mode"
    assert all(cancel_idx < idx for idx in display_idxs), "polling must happen after cancel"
    assert all(idx < literal_idx for idx in display_idxs), "polling must complete before -l"
    # Must have polled at least twice (mode was 1 for first two calls)
    assert len(display_idxs) >= 3, "must retry until pane_in_mode=0"


def test_wait_for_normal_mode_timeout_logs_warning(caplog):
    """If pane stays in copy-mode past max_retries, log a warning and return."""
    import logging

    def _always_in_mode(cmd, **kwargs):
        result = subprocess.CompletedProcess(args=cmd, returncode=0)
        result.stdout = "1"  # always in copy-mode
        return result

    with patch("subprocess.run", side_effect=_always_in_mode), \
         patch("time.sleep"), \
         caplog.at_level(logging.WARNING, logger="repowire.hooks.websocket_hook"):
        _wait_for_normal_mode("%5", max_retries=3, sleep_s=0.05)

    assert any("copy-mode" in r.message for r in caplog.records), \
        "must log a warning when copy-mode persists past timeout"
