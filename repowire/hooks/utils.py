"""Shared utilities for hook handlers."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from repowire.config.models import DEFAULT_DAEMON_URL

DAEMON_URL = os.environ.get("REPOWIRE_DAEMON_URL", DEFAULT_DAEMON_URL)


def get_pane_file(pane_id: str | None) -> str:
    """Normalize pane_id for use in cache filenames (strips % and path separators)."""
    sanitized = (pane_id or "unknown").replace("%", "").replace("/", "").replace("\\", "")
    return sanitized or "unknown"


def get_display_name() -> str:
    """Read daemon-assigned display name from REPOWIRE_DISPLAY_NAME env var.

    Set by session_handler after registering with the daemon.
    Falls back to cwd folder name if env var not set.
    """
    name = os.environ.get("REPOWIRE_DISPLAY_NAME")
    if name:
        return name
    return Path.cwd().name


def pending_cid_path(pane_id: str) -> Path:
    """Path to the pending correlation_id file for a pane."""
    return pane_logs_dir() / f"pending-{get_pane_file(pane_id)}.json"


def pane_logs_dir() -> Path:
    """Return the runtime log/state directory for pane-scoped hook files."""
    from repowire.config.models import CACHE_DIR

    path = CACHE_DIR / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ws_hook_lock_path(pane_id: str | None) -> Path:
    """Lock file guarding the single ws-hook owner for a pane."""
    return pane_logs_dir() / f"ws-hook-{get_pane_file(pane_id)}.lock"


def ws_hook_pid_path(pane_id: str | None) -> Path:
    """PID file for the background ws-hook process."""
    return pane_logs_dir() / f"ws-hook-{get_pane_file(pane_id)}.pid"


def ws_hook_meta_path(pane_id: str | None) -> Path:
    """JSON metadata for the active logical session in a pane."""
    return pane_logs_dir() / f"ws-hook-{get_pane_file(pane_id)}.meta.json"


def ws_hook_legacy_cwd_path(pane_id: str | None) -> Path:
    """Legacy cwd file retained for backward compatibility with older hooks/tests."""
    return pane_logs_dir() / f"ws-hook-{get_pane_file(pane_id)}.cwd"


def read_pane_runtime_metadata(pane_id: str | None) -> dict:
    """Read persisted metadata for the current pane owner."""
    meta_path = ws_hook_meta_path(pane_id)
    try:
        data = json.loads(meta_path.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    legacy_cwd = ws_hook_legacy_cwd_path(pane_id)
    try:
        cwd = legacy_cwd.read_text().strip()
    except OSError:
        cwd = ""
    return {"cwd": cwd} if cwd else {}


def write_pane_runtime_metadata(pane_id: str | None, metadata: dict) -> None:
    """Persist metadata for the active logical session in a pane."""
    meta_path = ws_hook_meta_path(pane_id)
    meta_path.write_text(json.dumps(metadata))

    cwd = metadata.get("cwd")
    if cwd:
        ws_hook_legacy_cwd_path(pane_id).write_text(str(cwd))


def clear_pending_cids(pane_id: str | None) -> None:
    """Remove any queued correlation IDs for a pane."""
    if not pane_id:
        return

    pending_path = pending_cid_path(pane_id)
    lock_path = pending_path.with_suffix(pending_path.suffix + ".lock")
    for path in (pending_path, lock_path):
        with suppress(OSError):
            path.unlink()


def clear_pane_runtime_state(pane_id: str | None) -> None:
    """Clear transient pane-scoped hook state after a pane dies or is taken over."""
    if not pane_id:
        return

    clear_pending_cids(pane_id)
    clear_ack_state(pane_id)
    for path in (
        ws_hook_pid_path(pane_id),
        ws_hook_meta_path(pane_id),
        ws_hook_legacy_cwd_path(pane_id),
    ):
        with suppress(OSError):
            path.unlink()


# --- beads-nfap.1: out-of-band ACK-receipt state ----------------------------
# The sender's hook records delivery receipts (AUTO-ACK / AUTO-NACK / intent-ACK)
# to a flock'd per-pane file instead of injecting them as conversation turns.
# Outgoing notifies register a pending correlation_id with a deadline; a watchdog
# in the ws-hook escalates the ones that go un-ACKed past their deadline.
#
# Structure:
#   {"pending":  {cid: {"deadline": float, "to_peer": str}},
#    "receipts": {cid: {"kind": "ack"|"nack"|"intent", "text": str}}}


def ack_state_path(pane_id: str | None) -> Path:
    """Path to the per-pane out-of-band ack-state file."""
    return pane_logs_dir() / f"ack-state-{get_pane_file(pane_id)}.json"


def _ack_state_lock_path(pane_id: str | None) -> Path:
    path = ack_state_path(pane_id)
    return path.with_suffix(path.suffix + ".lock")


def _with_ack_state_locked(
    pane_id: str | None, mutator: Callable[[dict[str, Any]], Any]
) -> Any:
    """Read-modify-write the ack-state file under an exclusive flock.

    Mirrors the flock discipline of _push_pending_cid / _pop_pending_cid so the
    ws-hook intercept, the watchdog sweeper and the MCP registration never race.
    """
    path = ack_state_path(pane_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _ack_state_lock_path(pane_id)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(path.read_text()) if path.exists() else {}
            except (json.JSONDecodeError, OSError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            state.setdefault("pending", {})
            state.setdefault("receipts", {})
            result = mutator(state)
            path.write_text(json.dumps(state))
            return result
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_ack_state(pane_id: str | None) -> dict[str, Any]:
    """Read the ack-state file (best-effort; empty skeleton if absent/corrupt)."""
    path = ack_state_path(pane_id)
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("pending", {})
    state.setdefault("receipts", {})
    return state


def register_pending_ack(
    pane_id: str | None, correlation_id: str, *, deadline: float, to_peer: str
) -> None:
    """Register an outgoing notify's correlation_id with a watchdog deadline."""

    def _mut(state: dict[str, Any]) -> None:
        state["pending"][correlation_id] = {"deadline": deadline, "to_peer": to_peer}

    _with_ack_state_locked(pane_id, _mut)


def resolve_pending_ack(
    pane_id: str | None, correlation_id: str, *, kind: str, text: str = ""
) -> bool:
    """Record an inbound receipt and clear its pending entry.

    Returns True if a matching pending entry existed (genuine delivery confirmation
    for a tracked notify), False otherwise (receipt for an untracked cid — still
    recorded for observability so the intent-ACK's extra context is not lost).
    """

    def _mut(state: dict[str, Any]) -> bool:
        existed = state["pending"].pop(correlation_id, None) is not None
        state["receipts"][correlation_id] = {"kind": kind, "text": text}
        return existed

    return bool(_with_ack_state_locked(pane_id, _mut))


def pop_overdue_acks(pane_id: str | None, *, now: float) -> list[dict[str, Any]]:
    """Atomically remove and return pending entries whose deadline has passed.

    Each returned dict carries correlation_id, to_peer and deadline. Removing them
    in the same locked transaction guarantees the watchdog escalates each overdue
    notify exactly once.
    """

    # Fast path: an idle pane (no registered pendings yet) must not cause the
    # watchdog tick to create an ack-state file on disk every interval.
    if not ack_state_path(pane_id).exists():
        return []

    def _mut(state: dict[str, Any]) -> list[dict[str, Any]]:
        overdue: list[dict[str, Any]] = []
        for cid, info in list(state["pending"].items()):
            if info.get("deadline", 0.0) <= now:
                overdue.append(
                    {
                        "correlation_id": cid,
                        "to_peer": info.get("to_peer", "unknown"),
                        "deadline": info.get("deadline", 0.0),
                    }
                )
                del state["pending"][cid]
        return overdue

    result = _with_ack_state_locked(pane_id, _mut)
    return result or []


def clear_ack_state(pane_id: str | None) -> None:
    """Remove the ack-state file and its lock for a pane."""
    if not pane_id:
        return
    for path in (ack_state_path(pane_id), _ack_state_lock_path(pane_id)):
        with suppress(OSError):
            path.unlink()


def _log_daemon_error(method: str, path: str, exc: Exception) -> None:
    """Log daemon request failure, including HTTP response body when available."""
    msg = f"repowire: daemon {method} {path} failed: {exc}"
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode(errors="ignore")
            if body:
                msg += f" - Body: {body}"
        except Exception:
            pass
    print(msg, file=sys.stderr)


def daemon_post(path: str, payload: dict, *, timeout: float = 2.0) -> dict | None:
    """POST JSON to daemon. Returns parsed response or None on failure."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{DAEMON_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        _log_daemon_error("POST", path, e)
        return None


def daemon_get(path: str, *, timeout: float = 2.0) -> dict | None:
    """GET from daemon. Returns parsed response or None on failure."""
    try:
        req = urllib.request.Request(f"{DAEMON_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        _log_daemon_error("GET", path, e)
        return None


def update_status(peer_identifier: str, status_value: str, *, use_pane_id: bool = False) -> bool:
    """Update peer status via daemon HTTP API."""
    if use_pane_id:
        payload = {"pane_id": peer_identifier, "status": status_value}
    else:
        payload = {"peer_name": peer_identifier, "status": status_value}
    result = daemon_post("/session/update", payload)
    return result is not None
