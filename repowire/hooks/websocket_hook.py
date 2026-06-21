"""Async WebSocket hook for Claude Code.

Maintains persistent WebSocket connection to daemon, injects queries via tmux,
and forwards responses via WebSocket. Fully reactive — no polling.
"""

import asyncio
import fcntl
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from contextlib import suppress

try:
    import httpx
except ImportError:  # pragma: no cover — httpx is a runtime dep of the hook
    httpx = None  # type: ignore[assignment]

try:
    import websockets
except ImportError as e:
    print(f"Missing dependency: {e}", file=sys.stderr)
    print("Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)

from repowire.client_epoch import compute_client_epoch
from repowire.config.models import AgentType
from repowire.hooks._identity import resolve_agent_path
from repowire.hooks._tmux import get_tmux_info, normalize_circle
from repowire.hooks.utils import (
    clear_pane_runtime_state,
    escalation_text,
    get_display_name,
    pending_cid_path,
    read_pane_runtime_metadata,
    receipt_inline_enabled,
    resolve_pending_ack,
    sweep_overdue_acks,
    tmux_send_keys,
    wait_for_normal_mode,
    write_pane_runtime_metadata,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# beads-nfap.2: the ACK-watchdog escalation helpers + tmux injector moved to
# repowire.hooks.utils so the stop-hook sweep can reuse them without importing
# this websockets-dependent module. These underscore aliases preserve the
# original call sites and the existing tests that patch them on this module.
_escalation_text = escalation_text
_receipt_inline_enabled = receipt_inline_enabled
_tmux_send_keys = tmux_send_keys
_wait_for_normal_mode = wait_for_normal_mode

# Set once at startup in main() — guards against pane reuse by a different agent
_expected_command: str | None = None

# Reconnect backoff cap — env-overridable so regression tests can compress the
# >250s daemon-down window that used to exhaust the old 50-attempt cap.
_RECONNECT_CAP_SEC = float(os.environ.get("REPOWIRE_WS_RECONNECT_CAP_SEC", "30"))


def _compute_backoff(attempt: int, cap: float = _RECONNECT_CAP_SEC, base: float = 1.0) -> float:
    """Full-jitter capped exponential backoff.

    Returns a delay in [0, min(cap, base * 2**attempt)]. Full jitter spreads
    simultaneous peer reconnects after a long daemon outage (anti
    thundering-herd / reconnect-storm — same class as q2ok singleton-conflict).
    """
    ceiling = min(cap, base * (2 ** attempt))
    return random.uniform(0.0, ceiling)


# Mirror agent-gateway._check_marker freshness window. A marker older than this
# is treated as crash-after-write, not an intentional signal.
_INTENTIONAL_MARKER_MAX_AGE_SEC = 300


def _marker_dir(role: str):
    """marker directory for a role: $HOME/ai-infra/ops/<role>/."""
    from pathlib import Path

    return Path(os.path.expanduser("~")) / "ai-infra" / "ops" / role


def _marker_present(role: str | None) -> bool:
    """Peek (no unlink) for a fresh intentional shutdown/restart marker.

    PEEK-ONLY: the marker is one-shot consumed by agent-gateway.monitor_loop;
    the hook must NOT unlink it. Returns True iff a fresh (<300s)
    .shutdown-intentional or .restart-intentional exists for `role`.
    role=None (no role env, see Task 0) → False (degrade to pane-safety only).
    """
    if not role:
        return False
    base = _marker_dir(role)
    for name in (".shutdown-intentional", ".restart-intentional"):
        marker = base / name
        try:
            age = time.time() - marker.stat().st_mtime
        except (FileNotFoundError, OSError):
            continue
        if age <= _INTENTIONAL_MARKER_MAX_AGE_SEC:
            return True
    return False


# --- beads-rz1g: client-refresh marker ------------------------------------
#
# The ws-hook loaded the installed package once at spawn. After a reinstall +
# daemon restart, this running process still executes the OLD in-memory client
# code. We detect that via the epoch (see repowire.client_epoch) and react by
# dropping a .refresh-pending marker — NEVER restarting here (that would be
# mid-turn). The stop-hook consumes the marker at a safe turn boundary.

# Captured once at this process's startup (in main), then cached. NOT recomputed
# from disk later — a reinstall must not retroactively change what a running
# process reports as its loaded code version.
_loaded_client_epoch: str | None = None


def _get_loaded_epoch() -> str:
    """The client epoch captured at THIS hook process's startup (cached)."""
    global _loaded_client_epoch
    if _loaded_client_epoch is None:
        _loaded_client_epoch = compute_client_epoch()
    return _loaded_client_epoch


def _write_refresh_pending(role: str, *, target_epoch: str, reason: str, scope: str) -> None:
    """Atomically drop a ``.refresh-pending`` marker for ``role``.

    Lands next to the other intentional markers ($HOME/ai-infra/ops/<role>/),
    reusing the existing marker machinery. tmp→rename so the stop-hook never
    reads a half-written marker. Best-effort: I/O errors are swallowed (a missed
    refresh self-heals on the next signal or the handshake of the next reconnect).
    """
    base = _marker_dir(role)
    try:
        base.mkdir(parents=True, exist_ok=True)
        marker = base / ".refresh-pending"
        tmp = base / ".refresh-pending.tmp"
        payload = json.dumps(
            {"target_epoch": target_epoch, "reason": reason, "scope": scope}
        )
        tmp.write_text(payload)
        os.replace(tmp, marker)
    except OSError as e:  # pragma: no cover — best-effort marker write
        logger.debug("refresh-pending write failed: %s", e)


def _handle_refresh_signal(target_epoch: str, reason: str, scope: str) -> bool:
    """React to a refresh signal (WS ``refresh`` message OR a stale handshake).

    Idempotent: a session already at ``target_epoch`` is a no-op. Never restarts
    here — only writes the marker the stop-hook consumes at a safe boundary.
    Degrades to no-op when the role-dir is unknown (standalone install without
    the brain ops layer). Returns True iff a marker was written.
    """
    if target_epoch and target_epoch == _get_loaded_epoch():
        return False
    role = _resolve_agent_role()
    if not role:
        return False
    _write_refresh_pending(role, target_epoch=target_epoch, reason=reason, scope=scope)
    logger.info(
        "refresh-pending marked (target=%s scope=%s reason=%s)",
        target_epoch, scope, reason,
    )
    return True


# Surface the WS-lost warning only after the disconnect persists, so a
# momentary blip does not flap the indicator.
_WARN_AFTER_ATTEMPTS = 3
_warn_active = False


def _pane_warn_set(pane_id: str) -> None:
    """Show a visible WS-lost warning in the pane WITHOUT touching stdin.

    Persistent indicator via pane title + a one-shot transient status message.
    Best-effort: tmux errors are swallowed and never break the reconnect loop.
    NEVER use send-keys/paste-buffer/display-popup (would corrupt Claude's turn).
    """
    global _warn_active
    try:
        subprocess.run(
            ["tmux", "select-pane", "-t", pane_id, "-T", "⚠ repowire WS lost"],
            capture_output=True,
        )
        if not _warn_active:
            subprocess.run(
                ["tmux", "display-message", "-t", pane_id,
                 "repowire: WS соединение потеряно, переподключаюсь…"],
                capture_output=True,
            )
    except Exception as e:  # pragma: no cover
        logger.debug("pane_warn_set failed: %s", e)
    _warn_active = True


def _pane_warn_clear(pane_id: str) -> None:
    """Clear the WS-lost indicator on successful reconnect. Best-effort."""
    global _warn_active
    if not _warn_active:
        return
    try:
        subprocess.run(
            ["tmux", "select-pane", "-t", pane_id, "-T", ""],
            capture_output=True,
        )
    except Exception as e:  # pragma: no cover
        logger.debug("pane_warn_clear failed: %s", e)
    _warn_active = False


class PaneUnsafeError(RuntimeError):
    """Raised when the pane no longer belongs to the expected live agent."""


# --- beads-61w: auto-ACK / auto-NACK receipts -------------------------------

_NOTIF_ID_IN_TEXT_RE = re.compile(r"\[#(notif-[a-f0-9]{8})\]")
_AUTO_ACK_PREFIXES = ("[AUTO-ACK]", "[AUTO-NACK]")

# beads-nfap.1: out-of-band receipt intercept (sender side).
# A bare correlation id, e.g. the `notif-XXX` right after `[AUTO-ACK] `.
_BARE_CID_RE = re.compile(r"(notif-[a-f0-9]{8})")
# intent-ACK authored by the receiver-LLM. It travels through MCP so it arrives
# wrapped in the intent-ACK's OWN `[#notif-NEW] ` prefix; the cid we resolve is
# the ORIGINAL delegation cid inside `ACK notif-ORIG …`.
_INTENT_ACK_RE = re.compile(r"^(?:\[#notif-[a-f0-9]{8}\]\s*)?ACK\s+(notif-[a-f0-9]{8})\b")


def _classify_receipt(text: str) -> tuple[str, str] | None:
    """Classify an inbound message as a delivery receipt.

    Returns (kind, correlation_id) where kind is 'ack' | 'nack' | 'intent', or
    None when the text is an ordinary message. The cid is the one whose delivery
    the receipt confirms (for intent-ACK that is the ORIGINAL delegation cid, not
    the intent-ACK's own wrapper cid).
    """
    if text.startswith("[AUTO-ACK]"):
        m = _BARE_CID_RE.search(text[:64])
        return ("ack", m.group(1) if m else "")
    if text.startswith("[AUTO-NACK]"):
        m = _BARE_CID_RE.search(text[:64])
        return ("nack", m.group(1) if m else "")
    m = _INTENT_ACK_RE.match(text)
    if m:
        return ("intent", m.group(1))
    return None


def _swallow_receipt(pane_id: str, kind: str, correlation_id: str, text: str) -> None:
    """Record a receipt to the per-pane ack-state file (no pane injection)."""
    if correlation_id:
        resolve_pending_ack(pane_id, correlation_id, kind=kind, text=text)


async def _intercept_receipt(pane_id: str, text: str) -> str | None:
    """Out-of-band receipt handling on the SENDER side.

    Records the receipt to ack-state. Returns the swallowed receipt kind
    ('ack' | 'intent') when the message was fully swallowed (complete silence, no
    injection). Returns None for ordinary messages, for AUTO-NACK (actionable →
    still injected, but recorded), and when the rollback flag forces inline
    behavior. The kind is returned (not a bare bool) so the caller can emit a
    reverse AUTO-ACK for a swallowed intent-ACK — see beads-eidq.
    """
    if _receipt_inline_enabled():
        return None
    receipt = _classify_receipt(text)
    if receipt is None:
        return None
    kind, correlation_id = receipt
    await asyncio.to_thread(_swallow_receipt, pane_id, kind, correlation_id, text)
    # AUTO-NACK is a genuine delivery failure — let it fall through to injection
    # so the sender still sees it. AUTO-ACK / intent-ACK are pure success noise.
    if kind == "nack":
        return None
    return kind


def _resolve_my_name() -> str:
    """Canonical display name for auto-ACK reverse notifies."""
    name = os.environ.get("REPOWIRE_DISPLAY_NAME") or os.environ.get("REPOWIRE_PEER_NAME")
    if name:
        return name
    try:
        return get_display_name()
    except Exception:
        return "unknown"


def _parse_correlation_id(text: str) -> str | None:
    """Extract notif-XXXXXXXX from a `[#notif-XXX] …` prefix. Scans first 64 chars only."""
    m = _NOTIF_ID_IN_TEXT_RE.match(text[:64])
    return m.group(1) if m else None


def _should_emit_ack(
    *,
    from_peer: str,
    my_name: str,
    from_peer_role: str | None,
    text: str,
) -> bool:
    """Auto-ACK skip rules (beads-61w spec §5.4):
    - loop-prevention: do not ACK an incoming [AUTO-ACK]/[AUTO-NACK]
    - self-addressed noop: sender == receiver → skip
    - service-peer noop: telegram/brain-admin have no turn-concept
    """
    if text.startswith(_AUTO_ACK_PREFIXES):
        return False
    if from_peer and from_peer == my_name:
        return False
    if from_peer_role == "service":
        return False
    return True


async def _daemon_post(path: str, body: dict) -> None:
    """Fire-and-forget POST to daemon. Swallows errors — auto-ACK is best-effort."""
    if httpx is None:
        return
    daemon_host = os.environ.get("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    daemon_port = os.environ.get("REPOWIRE_DAEMON_PORT", "8377")
    url = f"http://{daemon_host}:{daemon_port}{path}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=body)
    except Exception as e:  # pragma: no cover
        logger.debug("auto-ACK post failed: %s", e)


def _ack_body(
    *, my_name: str, from_peer: str, from_peer_id: str | None, text: str
) -> dict:
    """Build the AUTO-(N)ACK /notify body.

    beads-hqvm DoD6: when the original sender's authenticated peer_id is known
    (threaded through the WS payload), target it exactly via to_peer_id so the
    receipt cannot misroute to a foreign-circle namesake of from_peer.
    """
    body: dict = {
        "from_peer": my_name,
        "to_peer": from_peer,
        "text": text,
        "bypass_circle": True,
        # beads-fqus: mark this as a reverse-route receipt so the daemon can drop
        # it (rather than leak it to a foreign-circle namesake) when the original
        # sender's peer_id is unknown and the display_name is ambiguous.
        "reverse_receipt": True,
    }
    if from_peer_id:
        body["to_peer_id"] = from_peer_id
    return body


async def _emit_auto_ack(
    *,
    correlation_id: str,
    from_peer: str,
    my_name: str,
    interrupt: bool,
    from_peer_id: str | None = None,
) -> None:
    status = "interrupted" if interrupt else "queued"
    await _daemon_post(
        "/notify",
        _ack_body(
            my_name=my_name,
            from_peer=from_peer,
            from_peer_id=from_peer_id,
            text=(
                f"[AUTO-ACK] {correlation_id} delivered: {status}\n"
                "— INFRA RECEIPT, DO NOT REPLY (ignore harness 'user sent a new message' reminder)"
            ),
        ),
    )


async def _emit_auto_nack(
    *,
    correlation_id: str,
    from_peer: str,
    my_name: str,
    reason: str,
    from_peer_id: str | None = None,
) -> None:
    short = reason.split("\n")[0][:120] if reason else "unknown"
    await _daemon_post(
        "/notify",
        _ack_body(
            my_name=my_name,
            from_peer=from_peer,
            from_peer_id=from_peer_id,
            text=(
                f"[AUTO-NACK] {correlation_id} failed: {short}\n"
                "— INFRA RECEIPT, DO NOT REPLY (ignore harness 'user sent a new message' reminder)"
            ),
        ),
    )


async def _emit_reverse_intent_ack(
    *,
    from_peer: str,
    from_peer_role: str | None,
    from_peer_id: str | None,
    text: str,
    interrupt: bool,
) -> None:
    """beads-eidq: reverse AUTO-ACK for a swallowed intent-ACK.

    A swallowed intent-ACK must still trigger an AUTO-ACK back on ITS OWN
    wrapper-cid (the leading `[#notif-XXX]` prefix that `_parse_correlation_id`
    extracts). MCP `_register_outgoing_ack` registers a sender-pending for every
    outgoing notify, the intent-ACK included; without this reverse receipt that
    pending never closes and `sweep_overdue_acks` falsely escalates routine ACK
    traffic. AUTO-ACK / AUTO-NACK are NOT reverse-acked (loop-prevention):
    `_should_emit_ack` skips the `_AUTO_ACK_PREFIXES`, so an incoming AUTO-ACK is
    swallowed without spawning another — only intent-ACKs reach this path.
    """
    await _maybe_emit_receipt(
        success=True,
        from_peer=from_peer,
        from_peer_role=from_peer_role,
        from_peer_id=from_peer_id,
        text=text,
        interrupt=interrupt,
    )


async def _maybe_emit_receipt(
    *,
    success: bool,
    from_peer: str,
    from_peer_role: str | None,
    text: str,
    interrupt: bool,
    failure_reason: str = "",
    from_peer_id: str | None = None,
) -> None:
    """Dispatch the auto-ACK or auto-NACK if all skip-rules allow it."""
    my_name = _resolve_my_name()
    if not _should_emit_ack(
        from_peer=from_peer,
        my_name=my_name,
        from_peer_role=from_peer_role,
        text=text,
    ):
        return
    correlation_id = _parse_correlation_id(text)
    if not correlation_id:
        return
    if success:
        await _emit_auto_ack(
            correlation_id=correlation_id,
            from_peer=from_peer,
            my_name=my_name,
            interrupt=interrupt,
            from_peer_id=from_peer_id,
        )
    else:
        await _emit_auto_nack(
            correlation_id=correlation_id,
            from_peer=from_peer,
            my_name=my_name,
            reason=failure_reason or "injection failed",
            from_peer_id=from_peer_id,
        )



# beads-nfap.1: how long the watchdog waits for an ACK before escalating, and
# how often it sweeps for overdue pendings. Env-overridable so tests stay fast.
_ACK_WATCHDOG_INTERVAL_SEC = float(
    os.environ.get("REPOWIRE_ACK_WATCHDOG_INTERVAL_SEC", "15")
)


def _run_ack_watchdog_once(pane_id: str, now: float) -> None:
    """One watchdog sweep: escalate every pending notify whose deadline passed.

    Delegates to the shared sweep_overdue_acks (beads-nfap.2) so the ws-hook
    watchdog and the stop-hook defense-in-depth sweep stay single-sourced. The
    pop is atomic, so each overdue notify is escalated exactly once even across
    repeated ticks and across the two sweepers. No-op under the inline rollback
    flag (handled inside the sweep).
    """
    sweep_overdue_acks(pane_id, now=now, inject=lambda text: _tmux_send_keys(pane_id, text))


async def _ack_watchdog_loop(pane_id: str) -> None:
    """Persistent sweeper task hosted by the ws-hook (the always-on driver).

    Ticks every _ACK_WATCHDOG_INTERVAL_SEC regardless of pane activity, so a
    silent sender still gets its escalation. Cancelled when the connection
    closes; restarted on reconnect (pendings live in the file, not in memory).
    """
    if _receipt_inline_enabled():
        return
    while True:
        await asyncio.sleep(_ACK_WATCHDOG_INTERVAL_SEC)
        try:
            await asyncio.to_thread(_run_ack_watchdog_once, pane_id, time.time())
        except Exception as e:  # pragma: no cover — watchdog must never crash the hook
            logger.debug("ack-watchdog sweep failed: %s", e)


def _push_pending_cid(pane_id: str, correlation_id: str) -> None:
    """Append a correlation_id to the pending file for a pane.

    Uses flock to prevent race with stop_handler's _pop_pending_cid.
    """
    path = pending_cid_path(pane_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            try:
                pending = json.loads(path.read_text()) if path.exists() else []
            except (json.JSONDecodeError, OSError):
                pending = []
            pending.append(correlation_id)
            path.write_text(json.dumps(pending))
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)



def _get_pane_command(pane_id: str) -> str | None:
    """Get the current command running in a tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_current_command}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        cmd = result.stdout.strip().lower()
        return cmd if cmd else None
    except FileNotFoundError:
        return None


def _is_pane_safe(pane_id: str) -> bool:
    """Check if the tmux pane still has the expected agent process running.

    If _expected_command is set (module-level, captured at startup), the pane
    is only safe when the same command is still running. Falls back to a shell
    denylist otherwise.
    """
    shell_commands = {"bash", "zsh", "sh", "fish", "tcsh", "csh", "dash", "login"}
    cmd = _get_pane_command(pane_id)
    if not cmd:
        return False
    if _expected_command:
        return cmd == _expected_command
    return cmd not in shell_commands


async def handle_message(data: dict, pane_id: str, websocket=None) -> None:
    """Handle incoming WebSocket message.

    Args:
        data: Message data
        pane_id: Tmux pane ID
        websocket: WebSocket connection (for sending error responses)
    """
    msg_type = data.get("type")

    # Safety: verify agent is still running in the pane before injecting text
    needs_safety = msg_type in ("query", "notify", "broadcast")
    if needs_safety and not await asyncio.to_thread(_is_pane_safe, pane_id):
        logger.warning(f"Pane {pane_id} not safe for injection, dropping {msg_type}")
        if msg_type == "query" and websocket:
            correlation_id = data.get("correlation_id", "")
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "correlation_id": correlation_id,
                            "error": f"Pane {pane_id} not safe for injection",
                        }
                    )
                )
            except Exception:
                pass
        raise PaneUnsafeError(f"Pane {pane_id} no longer matches the expected agent")

    interrupt = bool(data.get("interrupt", False))
    from_peer_role = data.get("from_peer_role")
    # beads-hqvm DoD6: authenticated sender peer_id, used to address the
    # AUTO-(N)ACK back to the exact original sender (no display_name ambiguity).
    from_peer_id = data.get("from_peer_id")

    if msg_type == "query":
        correlation_id = data.get("correlation_id", "")
        from_peer = data.get("from_peer", "unknown")
        text = data.get("text", "")
        try:
            ok = await asyncio.to_thread(_tmux_send_keys, pane_id, text, interrupt)
            if ok:
                # Track pending correlation_id for stop hook response delivery
                _push_pending_cid(pane_id, correlation_id)
                logger.info(f"Injected query from {from_peer}: {correlation_id[:8]}")
                await _maybe_emit_receipt(
                    success=True,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    from_peer_id=from_peer_id,
                    text=text,
                    interrupt=interrupt,
                )
            else:
                error_msg = f"Failed to send keys to pane {pane_id}"
                logger.error(error_msg)
                if websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "error",
                                "correlation_id": correlation_id,
                                "error": error_msg,
                            }
                        )
                    )
                await _maybe_emit_receipt(
                    success=False,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    from_peer_id=from_peer_id,
                    text=text,
                    interrupt=interrupt,
                    failure_reason=error_msg,
                )
                if not await asyncio.to_thread(_is_pane_safe, pane_id):
                    raise PaneUnsafeError(error_msg)
        except PaneUnsafeError:
            raise
        except Exception as e:
            logger.error(f"Failed to inject query: {e}")
            if websocket:
                try:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "error",
                                "correlation_id": correlation_id,
                                "error": str(e),
                            }
                        )
                    )
                except Exception:
                    pass
            await _maybe_emit_receipt(
                success=False,
                from_peer=from_peer,
                from_peer_role=from_peer_role,
                from_peer_id=from_peer_id,
                text=text,
                interrupt=interrupt,
                failure_reason=str(e),
            )
            if not await asyncio.to_thread(_is_pane_safe, pane_id):
                raise PaneUnsafeError(str(e)) from e

    elif msg_type == "notify":
        from_peer = data.get("from_peer", "unknown")
        text = data.get("text", "")
        swallowed = await _intercept_receipt(pane_id, text)
        if swallowed:
            logger.info("Swallowed out-of-band receipt from %s (no injection)", from_peer)
            if swallowed == "intent":
                await _emit_reverse_intent_ack(
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    from_peer_id=from_peer_id,
                    text=text,
                    interrupt=interrupt,
                )
            return
        try:
            ok = await asyncio.to_thread(
                _tmux_send_keys, pane_id, f"@{from_peer}: {text}", interrupt
            )
            if ok:
                logger.info(f"Injected notification from {from_peer}")
                await _maybe_emit_receipt(
                    success=True,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    from_peer_id=from_peer_id,
                    text=text,
                    interrupt=interrupt,
                )
            else:
                await _maybe_emit_receipt(
                    success=False,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    from_peer_id=from_peer_id,
                    text=text,
                    interrupt=interrupt,
                    failure_reason=f"send_keys failed for pane {pane_id}",
                )
        except Exception as e:
            logger.error(f"Failed to inject notification: {e}")
            await _maybe_emit_receipt(
                success=False,
                from_peer=from_peer,
                from_peer_role=from_peer_role,
                from_peer_id=from_peer_id,
                text=text,
                interrupt=interrupt,
                failure_reason=str(e),
            )

    elif msg_type == "broadcast":
        from_peer = data.get("from_peer", "unknown")
        text = data.get("text", "")
        swallowed = await _intercept_receipt(pane_id, text)
        if swallowed:
            logger.info("Swallowed out-of-band broadcast receipt from %s", from_peer)
            if swallowed == "intent":
                await _emit_reverse_intent_ack(
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    from_peer_id=from_peer_id,
                    text=text,
                    interrupt=interrupt,
                )
            return
        try:
            msg = f"@{from_peer} [broadcast]: {text}"
            ok = await asyncio.to_thread(_tmux_send_keys, pane_id, msg, interrupt)
            if ok:
                logger.info(f"Injected broadcast from {from_peer}")
                await _maybe_emit_receipt(
                    success=True,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    from_peer_id=from_peer_id,
                    text=text,
                    interrupt=interrupt,
                )
            else:
                await _maybe_emit_receipt(
                    success=False,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    from_peer_id=from_peer_id,
                    text=text,
                    interrupt=interrupt,
                    failure_reason=f"send_keys failed for pane {pane_id}",
                )
        except Exception as e:
            logger.error(f"Failed to inject broadcast: {e}")
            await _maybe_emit_receipt(
                success=False,
                from_peer=from_peer,
                from_peer_role=from_peer_role,
                from_peer_id=from_peer_id,
                text=text,
                interrupt=interrupt,
                failure_reason=str(e),
            )

    elif msg_type == "refresh":
        # beads-rz1g: a deploy-time client-refresh signal. NOT a pane injection
        # (so it skips the pane-safety gate above) — we only write a marker the
        # stop-hook acts on at the next safe turn boundary.
        await asyncio.to_thread(
            _handle_refresh_signal,
            data.get("target_epoch", ""),
            data.get("reason", ""),
            data.get("scope", "workers"),
        )

    elif msg_type == "ping":
        pane_alive = await asyncio.to_thread(_is_pane_safe, pane_id)
        if websocket:
            try:
                tmux_info = await asyncio.to_thread(get_tmux_info)
                pong_circle = os.environ.get("REPOWIRE_CIRCLE") or normalize_circle(
                    tmux_info["session_name"]
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "pong",
                            "pane_alive": pane_alive,
                            "circle": pong_circle,
                        }
                    )
                )
            except Exception:
                pass
        if not pane_alive:
            logger.info(f"Pane {pane_id} dead on ping, exiting")
            raise PaneUnsafeError(f"Pane {pane_id} is no longer safe")


async def main() -> int:
    """Async hook that maintains WebSocket connection."""
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        logger.error("TMUX_PANE not set")
        return 1

    circle = (
        os.environ.get("REPOWIRE_CIRCLE")
        or normalize_circle(get_tmux_info()["session_name"])
        or "default"
    )
    display_name = get_display_name()
    backend_str = os.environ.get("REPOWIRE_BACKEND", "claude-code")
    try:
        backend = AgentType(backend_str)
    except ValueError:
        backend = AgentType.CLAUDE_CODE
    path = resolve_agent_path()

    # Snapshot pane command at startup to detect pane reuse
    global _expected_command
    _expected_command = _get_pane_command(pane_id)

    # beads-rz1g: capture the loaded client epoch NOW, at startup, while the
    # on-disk files still match the code this process imported. Caching it here
    # guarantees a later reinstall cannot retroactively mutate our loaded epoch.
    _get_loaded_epoch()

    daemon_host = os.environ.get("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    daemon_port = os.environ.get("REPOWIRE_DAEMON_PORT", "8377")
    uri = f"ws://{daemon_host}:{daemon_port}/ws"

    logger.info(f"Starting WebSocket hook for {display_name}@{circle} (pane={pane_id})")

    # Unbounded reconnect (beads-evl): the old 50-attempt cap let the hook die
    # forever during a >250s daemon outage, leaving the pane in an orchestration
    # deadzone. We now reconnect indefinitely while the pane stays alive; the
    # pane-safety guard (Claude gone → exit) replaces the attempt cap.
    attempt = 0

    # beads-nfap.1: the persistent out-of-band ACK watchdog. It reads the per-pane
    # ack-state file independently of the WS connection, so a single long-lived
    # task survives reconnects. No-op under the inline rollback flag.
    watchdog_task = asyncio.create_task(_ack_watchdog_loop(pane_id))
    try:
        return await _reconnect_loop(
            pane_id, uri, display_name, circle, backend, path, attempt
        )
    finally:
        watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await watchdog_task


async def _reconnect_loop(
    pane_id: str,
    uri: str,
    display_name: str,
    circle: str,
    backend: "AgentType",
    path: str,
    attempt: int,
) -> int:
    """Reconnect/message loop, extracted so main() can own the watchdog lifecycle."""
    while True:
        if not _is_pane_safe(pane_id):
            logger.info("Pane %s no longer safe, stopping reconnect loop", pane_id)
            clear_pane_runtime_state(pane_id)
            return 0
        try:
            async with websockets.connect(
                uri,
                ping_interval=20,
                ping_timeout=5,
            ) as websocket:
                attempt = 0
                _pane_warn_clear(pane_id)

                connect_msg: dict[str, str] = {
                    "type": "connect",
                    "display_name": display_name,
                    "circle": circle,
                    "backend": backend,
                    "path": path,
                    "pane_id": pane_id,
                    # Without this, the daemon's WS handler defaults role to
                    # AGENT on every reconnect, overwriting whatever role the
                    # peer originally registered with (session_handler.py
                    # pre-registers via HTTP /peers with the env-var role).
                    "role": os.environ.get("REPOWIRE_PEER_ROLE", "agent"),
                }
                peer_id = os.environ.get("REPOWIRE_PEER_ID")
                if peer_id:
                    connect_msg["peer_id"] = peer_id
                auth_token = os.environ.get("REPOWIRE_AUTH_TOKEN")
                if auth_token:
                    connect_msg["auth_token"] = auth_token
                await websocket.send(json.dumps(connect_msg))

                response = json.loads(await websocket.recv())
                if response.get("type") == "connected":
                    session_id = response["session_id"]
                    logger.info(f"Connected with session_id: {session_id}")
                    metadata = read_pane_runtime_metadata(pane_id)
                    metadata.update({
                        "backend": backend.value,
                        "cwd": path,
                        "display_name": response.get("display_name", display_name),
                        "peer_id": session_id,
                        # beads-rz1g: persist OUR loaded epoch so the stop-hook
                        # compares against what this session actually runs, not
                        # the (possibly newer) on-disk version.
                        "client_epoch": _get_loaded_epoch(),
                    })
                    write_pane_runtime_metadata(pane_id, metadata)

                    # beads-rz1g: the handshake carries the daemon's current
                    # deployed epoch. If we are stale (loaded != handshake), mark
                    # a refresh — this closes the race where the daemon broadcast
                    # went out before we reconnected after a daemon restart.
                    handshake_epoch = response.get("refresh_epoch")
                    if handshake_epoch:
                        await asyncio.to_thread(
                            _handle_refresh_signal,
                            handshake_epoch,
                            "reconnect-handshake",
                            "all",
                        )
                else:
                    logger.error(f"Unexpected response: {response}, retrying...")
                    await asyncio.sleep(2)
                    continue

                # Message loop — fully reactive, no polling tasks
                try:
                    async for message in websocket:
                        data = json.loads(message)
                        await handle_message(data, pane_id, websocket)
                except PaneUnsafeError as e:
                    logger.info("%s", e)
                    clear_pane_runtime_state(pane_id)
                    return 0

        except websockets.exceptions.ConnectionClosed as e:
            attempt += 1
            logger.warning(
                "Connection closed (attempt %d): code=%s", attempt, e.code
            )
        except (websockets.exceptions.WebSocketException, OSError) as e:
            attempt += 1
            logger.warning("Connection error (attempt %d): %s", attempt, e)

        if attempt >= _WARN_AFTER_ATTEMPTS:
            _pane_warn_set(pane_id)
        await asyncio.sleep(_compute_backoff(attempt))


def _resolve_agent_role() -> str | None:
    """Agent role-dir name for the marker path ($HOME/ai-infra/ops/<role>/).

    Resolved from BRAIN_AGENT_ROLE, which spawn-claude exports into the hook
    process env and which equals the role-dir name verbatim (director,
    devops-head, devops-worker — confirmed in beads-evl Task 0). Returns None
    when absent → _marker_present degrades to False (pane-safety guard still
    applies). NOTE: REPOWIRE_PEER_ROLE is the mesh role (agent/orchestrator),
    NOT the role-dir — do not use it here.
    """
    return os.environ.get("BRAIN_AGENT_ROLE") or None


def supervise() -> int:
    """Outer watchdog: re-enter main() on crash while the pane is alive and no
    intentional shutdown/restart is in progress. Defense-in-depth for the rare
    case where main() dies on an unhandled exception (unbounded reconnect
    already covers normal WS drops).
    """
    role = _resolve_agent_role()
    pane_id = os.environ.get("TMUX_PANE")
    while True:
        try:
            rc = asyncio.run(main())
        except KeyboardInterrupt:
            return 0
        except Exception:
            logger.exception("ws-hook main() crashed; evaluating respawn")
            rc = 1
        if rc == 0:
            return 0  # clean pane-unsafe exit from main()
        if pane_id and not _is_pane_safe(pane_id):
            logger.info("pane unsafe after crash; not respawning")
            return rc
        if _marker_present(role):
            logger.info("intentional marker present after crash; not respawning")
            return rc
        time.sleep(_compute_backoff(1))


if __name__ == "__main__":
    try:
        sys.exit(supervise())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
