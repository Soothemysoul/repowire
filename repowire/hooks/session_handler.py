#!/usr/bin/env python3
"""Handle SessionStart and SessionEnd hooks for auto-registration."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from repowire.config.models import load_config


def get_tmux_target() -> str | None:
    """Get current tmux session:window from environment."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None

    try:
        # Use -t to target specific pane, not the active one
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", "#{session_name}:#{window_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return None


def get_peer_name(cwd: str) -> str:
    """Generate a peer name from the working directory (folder name)."""
    return Path(cwd).name


def main() -> int:
    """Main entry point."""
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return 0

    event = input_data.get("hook_event_name")
    session_id = input_data.get("session_id")
    cwd = input_data.get("cwd", os.getcwd())

    config = load_config()
    tmux_target = get_tmux_target()
    peer_name = get_peer_name(cwd)

    if event == "SessionStart":
        # Register or update peer - name is primary key
        config.add_peer(
            name=peer_name,
            path=cwd,
            tmux_session=tmux_target,
            session_id=session_id,
        )
    elif event == "SessionEnd":
        # On session end, just clear the session_id but keep the peer
        # The daemon will clean up stale peers based on tmux status
        if session_id:
            config.update_peer_session(peer_name, "")

    return 0


if __name__ == "__main__":
    sys.exit(main())
