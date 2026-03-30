#!/usr/bin/env python3
"""Stop / AfterAgent hook handler - captures responses and delivers to daemon."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

from urllib.parse import quote

from repowire.hooks._tmux import get_pane_id
from repowire.hooks.adapters import hook_output, normalize
from repowire.hooks.utils import daemon_get, daemon_post, derive_display_name, pending_cid_path, update_status
from repowire.session.transcript import extract_last_turn_pair, extract_last_turn_tool_calls


def _pop_pending_cid(pane_id: str) -> str | None:
    """Pop the oldest pending correlation_id for a pane, if any.

    Uses flock to prevent race with websocket_hook's _push_pending_cid.
    """
    path = pending_cid_path(pane_id)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                if not path.exists():
                    return None
                pending = json.loads(path.read_text())
                if not pending:
                    return None
                cid = pending.pop(0)
                path.write_text(json.dumps(pending))
                return cid
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    except (json.JSONDecodeError, OSError, IndexError):
        return None


def _post_chat_turn(
    peer_name: str,
    role: str,
    text: str,
    tool_calls: list[dict[str, str]] | None = None,
    pane_id: str | None = None,
) -> None:
    """Post a chat turn to the daemon for dashboard display. Best-effort."""
    payload: dict = {"peer": peer_name, "role": role, "text": text}
    if tool_calls:
        payload["tool_calls"] = tool_calls
    if pane_id:
        payload["pane_id"] = pane_id
    daemon_post("/events/chat", payload)


def main(backend: str = "claude-code") -> int:
    """Main entry point for stop hook."""
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(f"repowire stop: invalid JSON input: {e}", file=sys.stderr)
        return 0

    if input_data.get("stop_hook_active", False):
        return 0

    payload = normalize(input_data, backend)

    pane_id = get_pane_id()

    # Resolve canonical peer name: prefer pane-based lookup so that responses
    # from restarted sessions are still attributed to the registered peer name,
    # not the current session_id which may differ after a session restart.
    peer_display = derive_display_name(payload.session_id, payload.cwd or os.getcwd())
    if pane_id:
        result = daemon_get(f"/peers/by-pane/{quote(pane_id, safe='')}")
        if result and result.get("display_name"):
            peer_display = result["display_name"]

    # Mark peer as online when agent finishes processing
    if pane_id:
        if not update_status(pane_id, "online", use_pane_id=True):
            print(
                f"repowire stop: failed to update status for pane {pane_id}",
                file=sys.stderr,
            )
    else:
        if not update_status(peer_display, "online"):
            print(
                f"repowire stop: failed to update status for {peer_display}",
                file=sys.stderr,
            )

    # Get response text: adapter extracts from agent-specific fields,
    # fall back to transcript parsing for Claude Code
    assistant_text = payload.response_text
    user_text = None
    tool_calls: list = []

    if payload.transcript_path:
        transcript_path = Path(payload.transcript_path).expanduser().resolve()
        user_text, transcript_text = extract_last_turn_pair(transcript_path)
        if transcript_text:
            assistant_text = transcript_text
        tool_calls = extract_last_turn_tool_calls(transcript_path) if assistant_text else []

    # Strip whitespace-only texts to prevent empty chat bubbles
    if user_text and not user_text.strip():
        user_text = None
    if assistant_text and not assistant_text.strip():
        assistant_text = None

    if user_text:
        _post_chat_turn(peer_display, "user", user_text, pane_id=pane_id)
    if assistant_text:
        _post_chat_turn(
            peer_display, "assistant", assistant_text, tool_calls or None, pane_id=pane_id,
        )

    # Deliver response to daemon for query resolution
    if pane_id and assistant_text:
        resp_payload: dict = {"pane_id": pane_id, "text": assistant_text}
        cid = _pop_pending_cid(pane_id)
        if cid:
            resp_payload["correlation_id"] = cid
        daemon_post("/response", resp_payload)

    hook_output(backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
