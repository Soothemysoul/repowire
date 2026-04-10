---
name: notification-interruption-bug
description: Bare Escape in _tmux_send_keys interrupts mid-tool-call agents; hex ESC[201~ is the correct fix
type: project
---

A bare Escape keystroke in `_tmux_send_keys` (`repowire/hooks/websocket_hook.py`) triggers TUI actions (confirm:no, chat:cancel) when a Claude Code agent is mid-tool-call, cancelling the in-progress action.

**Why:** The Escape was added as part of a fix for bracketed paste mode swallowing Enter after `tmux send-keys -l` (borrowed from the Gastown `NudgeSession` pattern). It replaced an earlier, more surgical hex-byte approach that sent `ESC[201~` to explicitly close paste mode. The bare Escape closes paste mode too, but also gets interpreted by the TUI as a cancel key.

**How to apply:** Use the hex `ESC[201~` approach (`tmux send-keys -H 1b 5b 32 30 31 7e`) to close paste mode without triggering TUI keybindings. Verify with `cat -v` for byte-level correctness, and live-test against the Claude Code TUI before changing the code. Notifications intentionally bypass BUSY checks (by design, mimics human input), so the interruption path is easy to hit.
