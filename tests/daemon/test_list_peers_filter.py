"""Tests for GET /peers circle filter."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from repowire.daemon.routes import peers as peers_route
from repowire.protocol.peers import PeerStatus, PeerRole


@pytest.fixture
def fake_registry(monkeypatch):
    """Provide a peer_registry with three peers in two circles."""

    class FakePeer:
        def __init__(self, name, circle):
            self.name = name
            self.display_name = name
            self.circle = circle
            self.path = f"/{name}"
            self.status = PeerStatus.ONLINE
            self.description = ""
            self.metadata = {}
            self.peer_id = name
            self.backend = "test"
            self.role = PeerRole.AGENT
            self.last_seen = None
            self.machine = "testmachine"
            self.tmux_session = None

    peers = [
        FakePeer("alpha", "global"),
        FakePeer("beta", "project-foo"),
        FakePeer("gamma", "global"),
    ]

    fake = MagicMock()
    fake.lazy_repair = AsyncMock()
    fake.get_all_peers = AsyncMock(return_value=peers)
    monkeypatch.setattr(peers_route, "get_peer_registry", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_list_peers_no_circle_returns_all(fake_registry):
    response = await peers_route.list_peers(circle=None, _=None)
    names = sorted(p.name for p in response.peers)
    assert names == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_list_peers_filters_by_circle(fake_registry):
    response = await peers_route.list_peers(circle="global", _=None)
    names = sorted(p.name for p in response.peers)
    assert names == ["alpha", "gamma"]


@pytest.mark.asyncio
async def test_list_peers_filter_returns_empty_for_unknown_circle(fake_registry):
    response = await peers_route.list_peers(circle="nonexistent", _=None)
    assert response.peers == []
