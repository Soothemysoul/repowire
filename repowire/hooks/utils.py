"""Shared utilities for hook handlers."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from repowire.config.models import DEFAULT_DAEMON_URL

DAEMON_URL = os.environ.get("REPOWIRE_DAEMON_URL", DEFAULT_DAEMON_URL)

logger = logging.getLogger(__name__)


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


def marker_dir(role: str) -> Path:
    """Per-role intentional-marker directory: ``$HOME/ai-infra/ops/<role>/``.

    Home of ``.shutdown-intentional`` / ``.restart-intentional`` /
    ``.refresh-pending`` (beads-rz1g) — the signals the agent-gateway and the
    hooks coordinate restarts through. Shared so the stop-hook and the ws-hook
    agree on the path without the stop-hook importing the websockets-dependent
    ws-hook module.
    """
    return Path(os.path.expanduser("~")) / "ai-infra" / "ops" / role


def resolve_agent_role() -> str | None:
    """Role-dir name for the marker path, from ``BRAIN_AGENT_ROLE`` (spawn-claude).

    Returns None when absent → marker logic degrades to a no-op (standalone
    install without the brain ops layer). NOTE: ``REPOWIRE_PEER_ROLE`` is the
    mesh role (agent/orchestrator), NOT the role-dir — do not use it here.
    """
    return os.environ.get("BRAIN_AGENT_ROLE") or None


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


def peek_overdue_acks(pane_id: str | None, *, now: float) -> list[dict[str, Any]]:
    """Return pending entries whose deadline has passed WITHOUT removing them.

    The non-destructive counterpart of ``pop_overdue_acks`` (beads-lfn6). The
    grace-backoff sweep peeks first so it can probe each receiver's liveness
    outside the flock (network I/O must not run under the lock), then finalizes
    the escalate-vs-re-arm decision atomically. Each dict carries correlation_id,
    to_peer, deadline and grace_count.
    """
    if not ack_state_path(pane_id).exists():
        return []

    def _mut(state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "correlation_id": cid,
                "to_peer": info.get("to_peer", "unknown"),
                "deadline": info.get("deadline", 0.0),
                "grace_count": int(info.get("grace_count", 0)),
            }
            for cid, info in state["pending"].items()
            if info.get("deadline", 0.0) <= now
        ]

    result = _with_ack_state_locked(pane_id, _mut)
    return result or []


def clear_ack_state(pane_id: str | None) -> None:
    """Remove the ack-state file and its lock for a pane."""
    if not pane_id:
        return
    for path in (ack_state_path(pane_id), _ack_state_lock_path(pane_id)):
        with suppress(OSError):
            path.unlink()


# --- ACK-watchdog escalation (shared by the ws-hook watchdog and stop-hook sweep) ---
#
# beads-nfap.1 introduced the always-on ws-hook watchdog; beads-nfap.2 adds a
# duplicate sweep at every Stop-hook turn boundary as defense-in-depth (covers a
# dead ws-hook process). Both paths share the helpers below so the escalation
# text and the no-double-escalation logic live in exactly one place.


def receipt_inline_enabled() -> bool:
    """Rollback switch (beads-nfap.1, mesh-safety): REPOWIRE_RECEIPT_INLINE=1 keeps
    the legacy inline pane-injection of receipts (and disables the watchdog),
    giving an instant revert if the out-of-band path misbehaves in production."""
    return os.environ.get("REPOWIRE_RECEIPT_INLINE", "") == "1"


def escalation_text(correlation_id: str, to_peer: str) -> str:
    """Escalation for a likely delivery FAILURE (receiver offline/unreachable).

    Used when the watchdog could not confirm the receiver is alive — the notify
    may genuinely not have been delivered, so the actionable advice is to check
    the receiver and resend.
    """
    return (
        f"[repowire] notify {correlation_id} → {to_peer} не подтверждён "
        "(нет ACK о доставке за отведённое время). Доставка могла не пройти — "
        "проверь получателя и при необходимости повтори отправку."
    )


def stalled_escalation_text(correlation_id: str, to_peer: str) -> str:
    """Escalation for a receiver that is ONLINE but kept silent past the grace cap.

    beads-lfn6: distinct from ``escalation_text`` — here delivery almost certainly
    succeeded (the message is queued for an online receiver), but no ACK arrived
    even after the full grace window. That is an anomaly worth surfacing (very long
    turn, or a broken receipt path) WITHOUT the misleading "delivery may have
    failed" framing that would re-invite redundant resends.
    """
    return (
        f"[repowire] notify {correlation_id} → {to_peer} доставлен, но получатель "
        "online и долго не подтверждает (затянувшийся turn или сбой receipt-пути). "
        "Повтор обычно НЕ нужен — проверь получателя, если важно."
    )


# --- beads-lfn6: liveness-aware grace-backoff (remainder of eg5x FIX #2) -------
#
# A receiver that is online but BUSY mid-turn longer than the ACK deadline is NOT
# a delivery failure — its intent-ACK is merely late. Escalating it is a
# false-positive that provokes a redundant resend. Before escalating an overdue
# pending the sweep probes the receiver's liveness: a live receiver earns a
# bounded grace extension instead of an escalation. The cap means a genuinely
# broken receipt path (live receiver that never confirms) is still surfaced
# eventually — grace never masks a real failure forever.
_ACK_GRACE_BACKOFF_SEC = float(os.environ.get("REPOWIRE_ACK_GRACE_BACKOFF_SEC", "60"))
_ACK_MAX_GRACE_ROUNDS = int(os.environ.get("REPOWIRE_ACK_MAX_GRACE_ROUNDS", "5"))
# Tight timeout so a slow/hung daemon never stalls a watchdog tick (ws-hook loop
# and stop-hook turn boundary both call the probe synchronously).
_ACK_LIVENESS_TIMEOUT_SEC = float(os.environ.get("REPOWIRE_ACK_LIVENESS_TIMEOUT_SEC", "2"))


def receiver_is_live(to_peer: str) -> bool:
    """Best-effort liveness probe for grace-backoff: is ``to_peer`` online/busy?

    Asks the daemon for the receiver's status. online/busy → live (the un-ACKed
    notify is a busy receiver, not a failure → grace). Fail-closed: offline /
    not-found / daemon-unreachable / probe error → not live → escalate, so a
    genuine delivery failure is never masked and the sweep never loops in grace
    forever. The GET uses a tight timeout so a hung daemon cannot stall the
    watchdog tick.
    """
    from urllib.parse import quote

    info = daemon_get(
        f"/peers/{quote(to_peer, safe='')}", timeout=_ACK_LIVENESS_TIMEOUT_SEC
    )
    if not info:
        return False
    return info.get("status") in ("online", "busy")


def _finalize_overdue_acks(
    pane_id: str | None,
    *,
    now: float,
    live_peers: dict[str, bool],
) -> list[dict[str, Any]]:
    """Atomically decide each still-overdue pending: re-arm (grace) or pop (escalate).

    Re-evaluated under the flock so a receipt that resolved an entry between the
    peek and now is honoured (the entry is gone → skipped), and so the two
    sweepers never double-act: whichever finalizes first either pops the entry
    (the other finds it gone) or extends its deadline beyond ``now`` (the other
    no longer sees it as overdue). A live receiver with grace remaining gets its
    deadline pushed out and grace_count bumped; otherwise it is popped and
    returned for escalation.
    """

    def _mut(state: dict[str, Any]) -> list[dict[str, Any]]:
        escalated: list[dict[str, Any]] = []
        for cid, info in list(state["pending"].items()):
            if info.get("deadline", 0.0) > now:
                continue
            to_peer = info.get("to_peer", "unknown")
            grace = int(info.get("grace_count", 0))
            live = live_peers.get(to_peer, False)
            if live and grace < _ACK_MAX_GRACE_ROUNDS:
                info["deadline"] = now + _ACK_GRACE_BACKOFF_SEC
                info["grace_count"] = grace + 1
            else:
                # "stalled": delivered to an online receiver that never confirmed
                # within the full grace window (long turn / broken receipt path) —
                # NOT a delivery failure, so it gets a non-alarming escalation.
                # "failed": receiver offline/unreachable → genuine delivery failure.
                escalated.append(
                    {
                        "correlation_id": cid,
                        "to_peer": to_peer,
                        "deadline": info.get("deadline", 0.0),
                        "reason": "stalled" if live else "failed",
                    }
                )
                del state["pending"][cid]
        return escalated

    result = _with_ack_state_locked(pane_id, _mut)
    return result or []


def sweep_overdue_acks(
    pane_id: str | None,
    *,
    now: float,
    inject: Callable[[str], Any],
    is_receiver_live: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Escalate every overdue un-ACKed pending exactly once, injecting via ``inject``.

    Shared by the ws-hook watchdog (always-on driver) and the stop-hook sweep
    (defense-in-depth). No-op under the REPOWIRE_RECEIPT_INLINE rollback flag
    (pendings are left untouched). Returns the escalated entries.

    Without ``is_receiver_live`` the legacy behavior holds: ``pop_overdue_acks``
    removes every overdue entry in one locked transaction (exactly-once across
    the two sweepers) and each is escalated.

    With ``is_receiver_live`` (beads-lfn6) the sweep peeks the overdue entries,
    probes each distinct receiver's liveness OUTSIDE the lock, then finalizes
    atomically: a live receiver with grace remaining is re-armed (deadline
    extended, no escalation); an offline/unreachable receiver — or one whose
    grace rounds are exhausted — is popped and escalated.
    """
    if receipt_inline_enabled():
        return []
    if is_receiver_live is None:
        overdue = pop_overdue_acks(pane_id, now=now)
        for entry in overdue:
            inject(escalation_text(entry["correlation_id"], entry.get("to_peer", "unknown")))
        return overdue

    candidates = peek_overdue_acks(pane_id, now=now)
    if not candidates:
        return []
    live_peers = {c["to_peer"]: bool(is_receiver_live(c["to_peer"])) for c in candidates}
    escalated = _finalize_overdue_acks(pane_id, now=now, live_peers=live_peers)
    for entry in escalated:
        cid = entry["correlation_id"]
        to_peer = entry.get("to_peer", "unknown")
        if entry.get("reason") == "stalled":
            inject(stalled_escalation_text(cid, to_peer))
        else:
            inject(escalation_text(cid, to_peer))
    return escalated


def wait_for_normal_mode(pane_id: str, max_retries: int = 20, sleep_s: float = 0.05) -> None:
    """Poll until pane exits copy-mode or timeout.

    Needed because tmux processes 'send-keys -X cancel' asynchronously — the pane
    may still report pane_in_mode=1 for a brief window after cancel is sent.
    Without this wait, the immediately following 'send-keys -l' arrives while
    copy-mode is still active and its characters are interpreted as vi commands
    (f=jump, t=jump-to, :=goto-line, /=search …) instead of literal input.
    """
    for _ in range(max_retries):
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_in_mode}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or result.stdout.strip() == "0":
            return
        time.sleep(sleep_s)
    logger.warning(
        "Pane %s still in copy-mode after %.1fs, injecting anyway",
        pane_id,
        max_retries * sleep_s,
    )


def tmux_send_keys(pane_id: str, text: str, interrupt: bool = False) -> bool:
    """Send keys to a tmux pane via subprocess.

    Default path (interrupt=False) mirrors direct-stdin semantics: cancel
    copy-mode if active, paste text via bracketed-paste, Enter. No Escape —
    Escape cancels Claude's in-flight turn, so sending it unconditionally
    turned every hook injection into an interrupt (beads-61w forensics).
    The tty buffer itself becomes the natural per-session message queue.

    interrupt=True re-adds Escape *before* paste so the cancel lands first
    and the paste is consumed as the receiver's next input (opt-in escape
    hatch for genuine emergencies).
    """
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "-X", "cancel"],
            capture_output=True,
        )
        wait_for_normal_mode(pane_id)
        if interrupt:
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "Escape"],
                capture_output=True,
                check=True,
            )
            time.sleep(0.1)
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "-l", text],
            capture_output=True,
            check=True,
        )
        time.sleep(0.5)
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "Enter"],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Failed to send keys to {pane_id}: {e}")
        return False


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
