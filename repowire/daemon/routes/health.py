"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from repowire.daemon.deps import get_peer_manager

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    backend: str
    relay_mode: bool = False


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Check daemon health status."""
    try:
        peer_manager = get_peer_manager()
        backend_name = peer_manager.backend_name
    except RuntimeError:
        backend_name = "unknown"

    # Get relay_mode from app state if available
    relay_mode = getattr(request.app.state, "relay_mode", False)

    return HealthResponse(
        status="ok",
        version="0.1.0",
        backend=backend_name,
        relay_mode=relay_mode,
    )
