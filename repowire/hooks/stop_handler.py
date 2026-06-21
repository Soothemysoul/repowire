#!/usr/bin/env python3
"""Stop / AfterAgent hook handler - captures responses and delivers to daemon."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from repowire.hooks._tmux import get_pane_id
from repowire.hooks.adapters import hook_output, normalize
from repowire.hooks.utils import (
    daemon_post,
    get_display_name,
    marker_dir,
    pending_cid_path,
    read_pane_runtime_metadata,
    resolve_agent_role,
    sweep_overdue_acks,
    tmux_send_keys,
    update_status,
)
from repowire.session.transcript import extract_last_turn_pair, extract_last_turn_tool_calls

# --- beads-rz1g: client-refresh at the turn boundary ------------------------
#
# The ws-hook drops a .refresh-pending marker when it learns the installed code
# changed (it never restarts mid-turn). The Stop hook fires BETWEEN turns, so
# this is the safe boundary to act: a busy session has just finished its turn.
# Orchestrators (director/pm) and scope=='advisory' are advisory (left to their
# own restart-overlay); an in-flight beads claim defers; otherwise self-restart
# via the existing detached `AGENT_RESTART=1 agent-stop` path, after a
# deterministic per-session jitter to avoid a reconnect storm.

_REFRESH_JITTER_WINDOW_SEC = int(os.environ.get("REPOWIRE_REFRESH_JITTER_SEC", "30"))
# Mirror the intentional-marker freshness window: a marker older than this is a
# leftover (deploy long past / clock skew) and is consumed without acting.
_REFRESH_MARKER_MAX_AGE_SEC = 300
_ORCHESTRATOR_ROLES = ("director", "pm")


def _refresh_jitter(peer_id: str, window: int = _REFRESH_JITTER_WINDOW_SEC) -> int:
    """Deterministic per-session jitter in ``[0, window)`` seconds before re-exec.

    Spreads the simultaneous respawn of many idle sessions after a deploy so they
    do not all reconnect to the freshly restarted daemon at once (thundering
    herd). Deterministic (hash of peer_id) → no daemon state, fully testable.
    """
    if window <= 0:
        return 0
    return int(hashlib.sha256(peer_id.encode()).hexdigest(), 16) % window


def _loaded_epoch_from_meta(pane_id: str | None) -> str | None:
    """The session's loaded client epoch, as persisted by the ws-hook on connect.

    Read from pane-meta (NOT recomputed from disk) so the idempotency check
    compares against the code this session actually runs, not the on-disk version
    a reinstall may already have bumped.
    """
    try:
        return read_pane_runtime_metadata(pane_id).get("client_epoch")
    except Exception:
        return None


def _read_refresh_marker(pane_id: str | None) -> dict | None:
    """Read a FRESH .refresh-pending marker for this role, else None.

    A stale marker (>_REFRESH_MARKER_MAX_AGE_SEC) is consumed and ignored. The
    returned dict carries the parsed payload plus ``_role`` for the caller.
    """
    role = resolve_agent_role()
    if not role:
        return None
    marker = marker_dir(role) / ".refresh-pending"
    try:
        age = time.time() - marker.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None
    if age > _REFRESH_MARKER_MAX_AGE_SEC:
        _consume_refresh_marker(role)
        return None
    try:
        data = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data["_role"] = role
    return data


def _consume_refresh_marker(role: str) -> None:
    """Remove the .refresh-pending marker (best-effort)."""
    try:
        (marker_dir(role) / ".refresh-pending").unlink()
    except OSError:
        pass


def _should_self_restart(role: str, scope: str) -> bool:
    """Scope/role decision (part 6).

    - ``advisory`` → never (left to the role's own restart-overlay);
    - orchestrators (director/pm) → never (always advisory, protect coordination);
    - ``all`` → every non-orchestrator role (workers AND heads);
    - ``workers`` → only ``*-worker`` roles.
    """
    if scope == "advisory":
        return False
    if role in _ORCHESTRATOR_ROLES:
        return False
    if scope == "all":
        return True
    if scope == "workers":
        return role.endswith("-worker")
    return False


def _has_inflight_claim() -> bool:
    """True iff this agent holds an in-progress beads claim → defer the refresh.

    A multi-turn claimed task must not be interrupted by a restart; the refresh
    is re-evaluated at every later turn boundary, so deferring is cheap. Keyed on
    the agent's assignee (``BD_ASSIGNEE`` or display name):
      - no identity / bd absent → False (nothing we can attribute; do not block);
      - bd error / unparseable → True (fail-closed: never interrupt when unsure);
      - else → True iff >=1 in-progress issue is assigned to us.
    """
    assignee = os.environ.get("BD_ASSIGNEE") or os.environ.get("REPOWIRE_DISPLAY_NAME")
    if not assignee:
        return False
    try:
        result = subprocess.run(
            ["bd", "list", "--status=in_progress", "--assignee", assignee, "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return True
    try:
        issues = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return True
    if isinstance(issues, dict):
        issues = issues.get("issues", [])
    return bool(issues)


def _trigger_refresh_restart(pane_id: str | None) -> None:
    """Self-restart at THIS safe turn boundary to load fresh client code.

    Mirrors the director self-restart path (ops config_fingerprint_check.py): a
    detached ``AGENT_RESTART=1 agent-stop "$SCOPE_NAME"`` — Gate 0 writes
    ``.restart-intentional`` so the agent-gateway respawns the session instead of
    treating the scope death as a crash, and the respawn re-injects the role's
    context. A per-session jitter precedes it. Degrades to a no-op without the
    ops scope env (standalone install). NEVER kills mid-turn — the Stop hook
    fires only between turns, so a busy session has already finished its turn.
    """
    scope_name = os.environ.get("SCOPE_NAME")
    if not scope_name:
        return
    peer_id = (
        os.environ.get("REPOWIRE_PEER_ID")
        or os.environ.get("REPOWIRE_DISPLAY_NAME")
        or scope_name
    )
    jitter = _refresh_jitter(peer_id)
    try:
        subprocess.Popen(
            [
                "setsid",
                "bash",
                "-c",
                f'sleep {jitter} && AGENT_RESTART=1 agent-stop "$SCOPE_NAME"',
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _maybe_trigger_refresh(pane_id: str | None) -> None:
    """Act on a .refresh-pending marker at the turn boundary (parts 5/6/7)."""
    data = _read_refresh_marker(pane_id)
    if data is None:
        return
    role = data["_role"]
    target_epoch = data.get("target_epoch", "")
    scope = data.get("scope", "workers")

    # Idempotency: a session already at target_epoch consumes the marker, no-op.
    loaded = _loaded_epoch_from_meta(pane_id)
    if target_epoch and loaded and target_epoch == loaded:
        _consume_refresh_marker(role)
        return

    # Scope/role: advisory roles never auto-restart — leave the marker for their
    # own restart-overlay (it self-GCs once stale).
    if not _should_self_restart(role, scope):
        return

    # Guard: never interrupt a multi-turn claimed task. Defer (leave the marker).
    if _has_inflight_claim():
        return

    _consume_refresh_marker(role)
    _trigger_refresh_restart(pane_id)


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


def _sweep_overdue_acks(pane_id: str) -> None:
    """Defense-in-depth duplicate of the ws-hook ACK-watchdog (beads-nfap.2).

    The ws-hook hosts the always-on watchdog, but if that process died between
    restarts or inside the supervise window, un-ACKed notifies would never
    escalate. The Stop hook fires at every turn boundary, so it re-sweeps the
    same per-pane ack-state. ``sweep_overdue_acks`` pops overdue entries
    atomically, so whichever sweeper (ws-hook or stop-hook) pops one first owns
    its single escalation — the two paths never double-escalate. No-op under the
    REPOWIRE_RECEIPT_INLINE rollback flag (handled inside the sweep).
    """
    sweep_overdue_acks(
        pane_id, now=time.time(), inject=lambda text: tmux_send_keys(pane_id, text)
    )


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

    # Mark peer as online when agent finishes processing
    peer_display = get_display_name()
    pane_id = get_pane_id()
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

    # beads-nfap.2: defense-in-depth re-sweep of overdue un-ACKed notifies at the
    # turn boundary, in case the ws-hook watchdog process is down.
    if pane_id:
        _sweep_overdue_acks(pane_id)

    # beads-rz1g: client-refresh — at this safe turn boundary, act on any
    # .refresh-pending marker the ws-hook dropped (self-restart to load fresh
    # code, with idempotency / advisory / in-flight-claim guards + jitter).
    _maybe_trigger_refresh(pane_id)

    hook_output(backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
