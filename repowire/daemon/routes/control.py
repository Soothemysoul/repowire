"""Control endpoints — privileged, deploy-driven operations (beads-rz1g).

``POST /control/refresh-clients`` is invoked by the atomic deploy procedure
(reinstall → restart daemon → refresh-clients → reaper) to tell every live
session that the installed client code changed and it should refresh (self-
restart at a safe turn boundary). Privileged: localhost-only (the deploy runs
locally) and bearer-auth when a daemon auth_token is configured — mirroring the
other mutating routes.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from repowire.client_epoch import compute_client_epoch
from repowire.daemon.auth import require_auth, require_localhost
from repowire.daemon.deps import get_app_state

router = APIRouter(tags=["control"])


class RefreshClientsRequest(BaseModel):
    """Body for POST /control/refresh-clients.

    ``target_epoch`` is optional: when omitted the daemon substitutes its own
    deployed epoch (a freshly restarted daemon is the authority on the deployed
    version), which eliminates deploy↔daemon epoch drift.
    """

    target_epoch: str | None = None
    reason: str = ""
    scope: Literal["workers", "all", "advisory"] = "workers"


class RefreshClientsResponse(BaseModel):
    """Reply: how many sessions the refresh reached + the epoch they converge to."""

    notified: int
    target_epoch: str


@router.post("/control/refresh-clients", response_model=RefreshClientsResponse)
async def refresh_clients(
    request: RefreshClientsRequest,
    _localhost: None = Depends(require_localhost),
    _auth: str | None = Depends(require_auth),
) -> RefreshClientsResponse:
    """Broadcast a client-refresh signal to all live sessions."""
    state = get_app_state()

    target_epoch = (
        request.target_epoch
        or getattr(state, "refresh_epoch", None)
        or compute_client_epoch()
    )
    # Record the new authoritative epoch so the WS handshake hands it to peers
    # that (re)connect after this push — closes the reconnect-after-restart race.
    state.refresh_epoch = target_epoch

    sent = await state.message_router.broadcast_refresh(
        target_epoch=target_epoch,
        reason=request.reason,
        scope=request.scope,
    )
    return RefreshClientsResponse(notified=len(sent), target_epoch=target_epoch)
