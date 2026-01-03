#!/usr/bin/env python3
"""Stop hook handler - captures responses and sends to daemon."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

SOCKET_PATH = "/tmp/repowire.sock"
PENDING_DIR = Path.home() / ".repowire" / "pending"


def get_tmux_target() -> str | None:
    """Get current tmux session:window from environment."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None

    try:
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


def tmux_to_filename(tmux_session: str) -> str:
    """Convert tmux session:window to safe filename."""
    return tmux_session.replace(":", "_").replace("/", "_")


def extract_last_assistant_response(transcript_path: Path) -> str | None:
    """Extract the last assistant response from a transcript."""
    if not transcript_path.exists():
        return None

    last_response = None
    with open(transcript_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    message = entry.get("message", {})
                    content = message.get("content", [])
                    if isinstance(content, list):
                        texts = [
                            c.get("text", "")
                            for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        ]
                        if texts:
                            last_response = " ".join(texts)
                    elif isinstance(content, str):
                        last_response = content
            except json.JSONDecodeError:
                continue

    return last_response


def send_to_daemon(correlation_id: str, response: str) -> bool:
    """Send a response to the daemon."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(SOCKET_PATH)

        message = json.dumps({
            "type": "hook_response",
            "correlation_id": correlation_id,
            "response": response,
        })
        sock.sendall(message.encode("utf-8") + b"\n")
        sock.close()
        return True
    except (socket.error, OSError):
        return False


def main() -> int:
    """Main entry point for stop hook."""
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return 0

    # Don't process if already in a hook chain
    if input_data.get("stop_hook_active", False):
        return 0

    transcript_path_str = input_data.get("transcript_path")
    if not transcript_path_str:
        return 0

    # Get tmux target from environment - this is stable across session restarts
    tmux_target = get_tmux_target()
    if not tmux_target:
        return 0

    # Check if there's a pending query for this tmux pane
    pending_filename = tmux_to_filename(tmux_target)
    pending_file = PENDING_DIR / f"{pending_filename}.json"
    if not pending_file.exists():
        return 0

    try:
        with open(pending_file, "r") as f:
            pending = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0

    correlation_id = pending.get("correlation_id")
    if not correlation_id:
        pending_file.unlink(missing_ok=True)
        return 0

    # Extract the response from transcript
    transcript_path = Path(transcript_path_str).expanduser()
    response = extract_last_assistant_response(transcript_path)

    if response:
        send_to_daemon(correlation_id, response)

    # Clean up pending file
    pending_file.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
