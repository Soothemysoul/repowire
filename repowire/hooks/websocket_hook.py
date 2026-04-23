"""Async WebSocket hook for Claude Code.

Maintains persistent WebSocket connection to daemon, injects queries via tmux,
and forwards responses via WebSocket. Fully reactive — no polling.
"""

import asyncio
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import time

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

from repowire.config.models import AgentType
from repowire.hooks._identity import resolve_agent_path
from repowire.hooks._tmux import get_tmux_info
from repowire.hooks.utils import (
    clear_pane_runtime_state,
    get_display_name,
    pending_cid_path,
    read_pane_runtime_metadata,
    write_pane_runtime_metadata,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set once at startup in main() — guards against pane reuse by a different agent
_expected_command: str | None = None


class PaneUnsafeError(RuntimeError):
    """Raised when the pane no longer belongs to the expected live agent."""


# --- beads-61w: auto-ACK / auto-NACK receipts -------------------------------

_NOTIF_ID_IN_TEXT_RE = re.compile(r"\[#(notif-[a-f0-9]{8})\]")
_AUTO_ACK_PREFIXES = ("[AUTO-ACK]", "[AUTO-NACK]")


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


async def _emit_auto_ack(
    *,
    correlation_id: str,
    from_peer: str,
    my_name: str,
    interrupt: bool,
) -> None:
    status = "interrupted" if interrupt else "queued"
    await _daemon_post(
        "/notify",
        {
            "from_peer": my_name,
            "to_peer": from_peer,
            "text": f"[AUTO-ACK] {correlation_id} delivered: {status}",
            "bypass_circle": True,
        },
    )


async def _emit_auto_nack(
    *,
    correlation_id: str,
    from_peer: str,
    my_name: str,
    reason: str,
) -> None:
    short = reason.split("\n")[0][:120] if reason else "unknown"
    await _daemon_post(
        "/notify",
        {
            "from_peer": my_name,
            "to_peer": from_peer,
            "text": f"[AUTO-NACK] {correlation_id} failed: {short}",
            "bypass_circle": True,
        },
    )


async def _maybe_emit_receipt(
    *,
    success: bool,
    from_peer: str,
    from_peer_role: str | None,
    text: str,
    interrupt: bool,
    failure_reason: str = "",
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
        )
    else:
        await _emit_auto_nack(
            correlation_id=correlation_id,
            from_peer=from_peer,
            my_name=my_name,
            reason=failure_reason or "injection failed",
        )



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


def _wait_for_normal_mode(pane_id: str, max_retries: int = 20, sleep_s: float = 0.05) -> None:
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


def _tmux_send_keys(pane_id: str, text: str, interrupt: bool = False) -> bool:
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
        _wait_for_normal_mode(pane_id)
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
                text=text,
                interrupt=interrupt,
                failure_reason=str(e),
            )
            if not await asyncio.to_thread(_is_pane_safe, pane_id):
                raise PaneUnsafeError(str(e)) from e

    elif msg_type == "notify":
        from_peer = data.get("from_peer", "unknown")
        text = data.get("text", "")
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
                    text=text,
                    interrupt=interrupt,
                )
            else:
                await _maybe_emit_receipt(
                    success=False,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
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
                text=text,
                interrupt=interrupt,
                failure_reason=str(e),
            )

    elif msg_type == "broadcast":
        from_peer = data.get("from_peer", "unknown")
        text = data.get("text", "")
        try:
            msg = f"@{from_peer} [broadcast]: {text}"
            ok = await asyncio.to_thread(_tmux_send_keys, pane_id, msg, interrupt)
            if ok:
                logger.info(f"Injected broadcast from {from_peer}")
                await _maybe_emit_receipt(
                    success=True,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
                    text=text,
                    interrupt=interrupt,
                )
            else:
                await _maybe_emit_receipt(
                    success=False,
                    from_peer=from_peer,
                    from_peer_role=from_peer_role,
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
                text=text,
                interrupt=interrupt,
                failure_reason=str(e),
            )

    elif msg_type == "ping":
        pane_alive = await asyncio.to_thread(_is_pane_safe, pane_id)
        if websocket:
            try:
                tmux_info = await asyncio.to_thread(get_tmux_info)
                pong_circle = os.environ.get("REPOWIRE_CIRCLE") or tmux_info["session_name"]
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

    circle = os.environ.get("REPOWIRE_CIRCLE") or get_tmux_info()["session_name"] or "default"
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

    daemon_host = os.environ.get("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    daemon_port = os.environ.get("REPOWIRE_DAEMON_PORT", "8377")
    uri = f"ws://{daemon_host}:{daemon_port}/ws"

    logger.info(f"Starting WebSocket hook for {display_name}@{circle} (pane={pane_id})")

    max_attempts = 50
    attempt = 0

    while attempt < max_attempts:
        try:
            async with websockets.connect(uri, ping_interval=None, ping_timeout=None) as websocket:
                attempt = 0

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
                    })
                    write_pane_runtime_metadata(pane_id, metadata)
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
                f"Connection closed (attempt {attempt}/{max_attempts}): code={e.code}, "
                f"reconnecting in 2s..."
            )
            await asyncio.sleep(2)

        except (websockets.exceptions.WebSocketException, OSError) as e:
            attempt += 1
            delay = min(1 * 2**attempt, 5)
            logger.warning(
                f"Connection error (attempt {attempt}/{max_attempts}): {e}, retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
            continue

        logger.info("Connection ended, reconnecting in 2s...")
        await asyncio.sleep(2)

    logger.error(f"Exhausted {max_attempts} reconnect attempts, exiting")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
