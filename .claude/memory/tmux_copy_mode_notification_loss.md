---
name: tmux-copy-mode-notification-loss
description: User scroll enters tmux copy-mode; send-keys -l gets intercepted as copy-mode commands, notification never reaches agent
type: project
---

# tmux copy-mode notification loss

## Symptom
When user scrolls in a tmux pane (enters copy-mode-vi), `tmux send-keys -l` keystrokes are intercepted as copy-mode commands (f, F, t, T, :, /, ? → jump/goto/search). The injected notification or query never reaches the agent until the user manually exits copy-mode.

## Fix (beads-xak, D1 + re-open)

### First fix: unconditional cancel
`_tmux_send_keys` calls `send-keys -X cancel` before `send-keys -l`. The `-X cancel` is a no-op outside copy-mode (tmux ≥ 2.4).

### Re-open: timing race (root cause of production regression)
tmux processes `send-keys -X cancel` asynchronously. Without a wait, the immediately following `send-keys -l` arrives while copy-mode is still active — characters are interpreted as vi commands (f=jump, :=goto-line, /=search). The helper `_wait_for_normal_mode()` polls `#{pane_in_mode}` up to 20× (50 ms each, 1 s max) between cancel and `-l`. On timeout it logs a warning and proceeds anyway.

## Regression
`tests/hooks/test_tmux_send_keys_copy_mode.py` covers:
- cancel precedes -l inject (order guarantee)
- cancel is always called exactly once
- full sequence (cancel → poll → -l → Escape → Enter) intact
- timing race: display-message polled ≥3× when mode stays 1, only then -l fired
- timeout path: warning logged when pane never exits copy-mode within retries
