---
name: tmux-copy-mode-notification-loss
description: User scroll enters tmux copy-mode; send-keys -l gets intercepted as copy-mode commands, notification never reaches agent
type: project
---

# tmux copy-mode notification loss

## Symptom
When user scrolls in a tmux pane (enters copy-mode-vi), `tmux send-keys -l` keystrokes are intercepted as copy-mode commands (f, F, t, T, :, /, ? → jump/goto/search). The injected notification or query never reaches the agent until the user manually exits copy-mode.

## Fix (beads-xak, D1)
`_tmux_send_keys` in `repowire/hooks/websocket_hook.py` now unconditionally calls `send-keys -X cancel` before `send-keys -l`. The `-X cancel` command is a no-op when the pane is in normal mode (tmux ≥ 2.4), so the fix is safe in all pane states. Conditional probing via `#{pane_in_mode}` was rejected as more complex with no reliability advantage.

## Regression
`tests/hooks/test_tmux_send_keys_copy_mode.py` covers:
- cancel precedes -l inject (order guarantee)
- cancel is always called exactly once
- full sequence (cancel → -l → Escape → Enter) intact
