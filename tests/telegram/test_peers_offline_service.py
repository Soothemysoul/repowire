"""Regression tests for beads-7ijt — Telegram /peers visibility.

``/peers`` must keep user-facing service peers (service/orchestrator/human)
selectable even when OFFLINE so the user can message them and the
agent-gateway respawns them on demand. Regular AGENT peers stay listed only
while online/busy.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from repowire.telegram.bot import TelegramPeer


def _make_peer(tmp_path: Path) -> TelegramPeer:
    return TelegramPeer(
        bot_token="0:fake",
        chat_id="999",
        daemon_url="http://127.0.0.1:8377",
        state_path=tmp_path / "telegram-state.json",
    )


def _peers_resp(peers: list[dict]) -> MagicMock:
    r = MagicMock()
    r.json.return_value = {"peers": peers}
    return r


_PEERS = [
    {"display_name": "director-claude-code", "role": "orchestrator",
     "status": "online", "path": "/agents/director"},
    {"display_name": "brain-admin-claude-code", "role": "service",
     "status": "offline", "path": "/agents/brain-admin"},
    {"display_name": "telegram-claude-code", "role": "service",
     "status": "online", "path": "/telegram"},
    {"display_name": "librarian-claude-code", "role": "agent",
     "status": "offline", "path": "/agents/librarian"},
    {"display_name": "backend-worker-claude-code", "role": "agent",
     "status": "online", "path": "/agents/backend-worker"},
]


@pytest.mark.asyncio
async def test_offline_service_peer_listed(tmp_path):
    peer = _make_peer(tmp_path)
    peer._http.get = AsyncMock(return_value=_peers_resp(_PEERS))
    peer._tg_send = AsyncMock()

    await peer._cmd_peers()

    peer._tg_send.assert_awaited_once()
    # Telegram MarkdownV2 escapes hyphens (``\-``); normalise before matching.
    text = peer._tg_send.await_args.args[0].replace("\\", "")
    # Offline service peer must be selectable.
    assert "brain-admin-claude-code" in text
    # Online peers (any role) stay listed.
    assert "director-claude-code" in text
    assert "telegram-claude-code" in text


@pytest.mark.asyncio
async def test_offline_agent_peer_hidden(tmp_path):
    peer = _make_peer(tmp_path)
    peer._http.get = AsyncMock(return_value=_peers_resp(_PEERS))
    peer._tg_send = AsyncMock()

    await peer._cmd_peers()

    text = peer._tg_send.await_args.args[0].replace("\\", "")
    # Offline AGENT peer is NOT user-facing → hidden.
    assert "librarian-claude-code" not in text
    # Online agent still shows.
    assert "backend-worker-claude-code" in text


@pytest.mark.asyncio
async def test_offline_service_peer_has_button(tmp_path):
    """The offline service peer must be a selectable inline button, not just
    a text line — that is what lets the user start a chat with it."""
    peer = _make_peer(tmp_path)
    peer._http.get = AsyncMock(return_value=_peers_resp(_PEERS))
    peer._tg_send = AsyncMock()

    await peer._cmd_peers()

    keyboard = peer._tg_send.await_args.args[1]
    targets = str(keyboard)
    assert "target:brain-admin-claude-code" in targets


@pytest.mark.asyncio
async def test_all_offline_agents_yields_no_peers_message(tmp_path):
    """When only offline AGENT peers exist, the list is empty."""
    peer = _make_peer(tmp_path)
    only_offline_agents = [
        {"display_name": "w1-claude-code", "role": "agent",
         "status": "offline", "path": "/agents/w1"},
    ]
    peer._http.get = AsyncMock(return_value=_peers_resp(only_offline_agents))
    peer._tg_send = AsyncMock()

    await peer._cmd_peers()

    text = peer._tg_send.await_args.args[0]
    assert "No peers online" in text
