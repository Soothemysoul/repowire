"""Spawn endpoints — create and kill agent sessions via tmux."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from repowire.config.models import AgentType
from repowire.daemon.auth import require_auth
from repowire.daemon.deps import get_config, get_peer_registry
from repowire.daemon.peer_registry import AmbiguousPeerError
from repowire.naming import build_base_display_name
from repowire.protocol.peers import PeerStatus
from repowire.spawn import SpawnConfig, SpawnResult, kill_peer, kill_peer_by_pane, spawn_peer

router = APIRouter(tags=["spawn"])

# In-memory registry of tmux_sessions spawned by this daemon instance.
# Only sessions in this set can be killed via /kill.
_spawned_sessions: set[str] = set()

# In-flight singleton spawns: (canonical_display_name, circle) -> Future[SpawnResponse].
# Subsequent callers for the same singleton key await the shared future instead of
# launching a second process.
_pending_spawns: dict[tuple[str, str], asyncio.Future[SpawnResponse]] = {}


class SpawnConfigResponse(BaseModel):
    """Spawn configuration for UI discovery."""

    enabled: bool
    allowed_commands: list[str] = []
    allowed_paths: list[str] = []


@router.get("/spawn/config", response_model=SpawnConfigResponse)
async def get_spawn_config(
    _: str | None = Depends(require_auth),
) -> SpawnConfigResponse:
    """Return spawn configuration so the UI can offer spawn controls."""
    cfg = get_config()
    cmds = cfg.daemon.spawn.allowed_commands
    paths = cfg.daemon.spawn.allowed_paths
    return SpawnConfigResponse(
        enabled=bool(cmds and paths),
        allowed_commands=cmds,
        allowed_paths=paths,
    )


class SpawnRequest(BaseModel):
    """Request to spawn a new agent session."""

    path: str = Field(..., description="Absolute path to the project directory")
    command: str = Field(..., description="Command to run — must be in allowed_commands")
    circle: str = Field(default="default", description="Circle to spawn into")
    wait_for_ready: bool = Field(default=False, description="Block until peer is ONLINE")
    ready_timeout_ms: int = Field(default=30000, description="Max wait in milliseconds")


class SpawnResponse(BaseModel):
    """Result of a successful spawn."""

    ok: bool = True
    display_name: str
    tmux_session: str
    elapsed_ms: int | None = None
    status: str = "spawning"


class KillRequest(BaseModel):
    """Request to kill a spawned session.

    Prefer peer_id or peer_name+circle for stable lookup (survives window
    rename). tmux_session is kept for back-compat with existing callers.
    """

    tmux_session: str | None = Field(
        None, description="Session ref returned by /spawn (e.g. 'default:myproject')"
    )
    peer_id: str | None = Field(None, description="Peer ID for registry-based kill")
    peer_name: str | None = Field(None, description="Peer display name for registry-based kill")
    circle: str | None = Field(None, description="Circle to disambiguate peer_name")


class KillResponse(BaseModel):
    """Result of a successful kill.

    beads-99oh: kill semantics are "bring the peer to OFFLINE", not "must kill a
    live pane". ``cleaned_registry`` reports the registry record was demoted to
    OFFLINE; ``pane_killed`` reports whether a live tmux pane was actually killed
    (False when the pane was already dead or the peer had no pane_id).
    """

    ok: bool = True
    cleaned_registry: bool = False
    pane_killed: bool = False


def _command_allowed(command: str, allowed: list[str]) -> bool:
    """Return True if command exactly matches an allowed entry, or starts
    with one of them followed by a space (so flags after the role are ok).
    """
    if not allowed:
        return False
    if command in allowed:
        return True
    return any(command.startswith(a + " ") for a in allowed)


def _validate_spawn_request(path: str, command: str) -> None:
    """Validate path and command against the spawn allowlists.

    Raises HTTPException 403 if spawn is disabled or either value is not allowed.
    Raises HTTPException 404 if the path does not exist on disk.
    """
    cfg = get_config()
    allowed_commands = cfg.daemon.spawn.allowed_commands
    allowed_paths = cfg.daemon.spawn.allowed_paths

    if not allowed_commands or not allowed_paths:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Spawn is disabled. Set daemon.spawn.allowed_commands and"
                " daemon.spawn.allowed_paths in ~/.repowire/config.yaml"
            ),
        )

    if not _command_allowed(command, allowed_commands):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Command not in allowed_commands: {command!r}",
        )

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path does not exist: {path}",
        )

    allowed_roots = [Path(p).expanduser().resolve() for p in allowed_paths]
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Path not under any allowed_paths: {path}",
        )


async def _do_spawn_and_wait(
    request: SpawnRequest,
    resolved_path: str,
    canonical_name: str,
) -> SpawnResponse:
    """Execute the actual tmux spawn and optionally wait for ready.

    Called only by the *first* caller for a given (canonical_name, circle) key.
    The result is stored in a shared Future so concurrent singleton requests
    can await it without launching a second process.
    """
    try:
        result: SpawnResult = spawn_peer(
            SpawnConfig(
                path=resolved_path,
                circle=request.circle,
                backend=AgentType.CLAUDE_CODE,
                command=request.command,
            )
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    _spawned_sessions.add(result.tmux_session)

    if not request.wait_for_ready:
        return SpawnResponse(display_name=canonical_name, tmux_session=result.tmux_session)

    peer_registry = get_peer_registry()
    event = peer_registry.register_spawn_waiter(canonical_name)
    start = time.monotonic()
    try:
        await asyncio.wait_for(event.wait(), timeout=request.ready_timeout_ms / 1000)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return SpawnResponse(
            display_name=canonical_name,
            tmux_session=result.tmux_session,
            elapsed_ms=elapsed_ms,
            status="online",
        )
    except asyncio.TimeoutError:
        peer_registry._spawn_ready_events.pop(canonical_name, None)
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail={
                "error": "timeout",
                "display_name": canonical_name,
                "elapsed_ms": request.ready_timeout_ms,
            },
        )


@router.post("/spawn", response_model=SpawnResponse)
async def spawn(
    request: SpawnRequest,
    _: str | None = Depends(require_auth),
) -> SpawnResponse:
    """Spawn a new agent coding session.

    Both the command and the path must be explicitly allowed in
    daemon.spawn.allowed_commands / allowed_paths in ~/.repowire/config.yaml.
    The spawned agent self-registers via its SessionStart hook once it starts.

    If wait_for_ready=True, the call blocks until the peer's WebSocket hook
    connects (peer is ONLINE) or ready_timeout_ms elapses (HTTP 408).

    For singleton roles (head/pm/orchestrator), concurrent spawn requests for
    the same (role, circle) are deduplicated:
    - If the peer is already ONLINE, returns its info immediately (status="existing").
    - If a spawn is in-flight, the caller shares the first caller's Future result.
    - If no peer exists, a fresh spawn is performed as normal.
    """
    _validate_spawn_request(request.path, request.command)

    resolved_path = str(Path(request.path).expanduser().resolve())
    role_name = Path(resolved_path).name
    cfg = get_config()
    canonical_name = build_base_display_name(resolved_path, AgentType.CLAUDE_CODE)

    # --- Singleton dedup ---
    singleton_roles = set(cfg.daemon.spawn.singleton_roles)
    if role_name in singleton_roles:
        peer_registry = get_peer_registry()

        # 1. Already live — return existing peer info, no spawn needed.
        #    beads-99oh: ONLINE/BUSY and a HEALTHY in-flight self-restart
        #    (RESTARTING within the restart cap, beads-k1b3) count as live and
        #    are deduped. A STUCK restart (RESTARTING past the cap — process
        #    gone, never returned) is NOT live: fall through and relaunch it
        #    instead of no-op'ing the respawn (the wedge this fixes).
        existing = await peer_registry.get_peer(canonical_name, circle=request.circle)
        if (
            existing is not None
            and existing.status != PeerStatus.OFFLINE
            and not peer_registry.is_restart_stuck(existing)
        ):
            return SpawnResponse(
                display_name=existing.display_name,
                tmux_session=existing.tmux_session or "",
                elapsed_ms=0,
                status="existing",
            )

        # 2. In-flight spawn for the same key — share the result.
        pending_key = (canonical_name, request.circle)
        pending_fut = _pending_spawns.get(pending_key)
        if pending_fut is not None and not pending_fut.done():
            try:
                shared = await asyncio.shield(pending_fut)
                return SpawnResponse(
                    display_name=shared.display_name,
                    tmux_session=shared.tmux_session,
                    elapsed_ms=shared.elapsed_ms,
                    status="existing",
                )
            except Exception:
                pass  # First spawn failed — fall through to fresh attempt

        # 3. No online peer, no in-flight spawn — we are the first caller.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[SpawnResponse] = loop.create_future()
        _pending_spawns[pending_key] = fut
        try:
            response = await _do_spawn_and_wait(request, resolved_path, canonical_name)
            fut.set_result(response)
            return response
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            _pending_spawns.pop(pending_key, None)

    # --- Non-singleton: normal path (workers, service roles) ---
    return await _do_spawn_and_wait(request, resolved_path, canonical_name)


@router.post("/kill", response_model=KillResponse)
async def kill(
    request: KillRequest,
    _: str | None = Depends(require_auth),
) -> KillResponse:
    """Kill a spawned agent session.

    Accepts either peer_id / peer_name+circle (registry lookup, stable across
    window renames) or legacy tmux_session (back-compat, only for sessions
    spawned via /spawn on this instance).
    """
    # Registry-based path: resolve peer → pane_id → kill by stable pane ref
    if request.peer_id or request.peer_name:
        peer_registry = get_peer_registry()
        identifier = request.peer_id or request.peer_name
        # beads-bof3: killing a namesake by name without a circle could hit the
        # wrong circle's peer — fail fast with an actionable error instead.
        try:
            peer = await peer_registry.get_peer(
                identifier, circle=request.circle, raise_ambiguous=True
            )
        except AmbiguousPeerError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e),
            )
        if peer is None:
            # Genuinely unknown identifier — nothing in the registry to clean.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Peer not found: {identifier}",
            )
        # beads-99oh: kill = "bring this peer to OFFLINE". Always demote the
        # registry record (even if the pane is already dead / there is no
        # pane_id) so a stuck RESTARTING record cannot survive a kill and
        # re-wedge the next spawn. The pane kill is best-effort on top.
        await peer_registry.mark_offline(peer.peer_id)
        pane_killed = kill_peer_by_pane(peer.pane_id) if peer.pane_id else False
        return KillResponse(cleaned_registry=True, pane_killed=pane_killed)

    # Legacy tmux_session path — back-compat, behavior unchanged
    if not request.tmux_session:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide tmux_session, peer_id, or peer_name",
        )
    if request.tmux_session not in _spawned_sessions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found or not spawned by repowire: {request.tmux_session}",
        )
    ok = kill_peer(request.tmux_session)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tmux session not found: {request.tmux_session}",
        )
    _spawned_sessions.discard(request.tmux_session)
    return KillResponse()
