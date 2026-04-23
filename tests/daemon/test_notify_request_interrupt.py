"""beads-61w: NotifyRequest.interrupt plumbing — daemon route / pydantic model."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from repowire.daemon.routes import messages
from repowire.daemon.routes.messages import NotifyRequest
from repowire.protocol.peers import PeerRole, PeerStatus


class TestNotifyRequestInterrupt:
    def test_interrupt_defaults_to_false(self):
        req = NotifyRequest(from_peer="a", to_peer="b", text="hi")
        assert req.interrupt is False

    def test_interrupt_explicit_true(self):
        req = NotifyRequest(from_peer="a", to_peer="b", text="hi", interrupt=True)
        assert req.interrupt is True

    def test_interrupt_explicit_false(self):
        req = NotifyRequest(from_peer="a", to_peer="b", text="hi", interrupt=False)
        assert req.interrupt is False


def _make_peer(display_name: str, role: PeerRole) -> MagicMock:
    p = MagicMock()
    p.display_name = display_name
    p.role = role
    p.status = PeerStatus.ONLINE
    p.peer_id = f"pid-{display_name}"
    return p


def _make_fake_registry(monkeypatch, from_peer, to_peer):
    """Patch get_peer_registry in routes.messages with controlled peer objects."""

    async def fake_get_peer(identifier, circle=None):
        if identifier == to_peer.display_name:
            return to_peer
        if identifier == from_peer.display_name:
            return from_peer
        return None

    fake = MagicMock()
    fake.lazy_repair = AsyncMock()
    fake.get_peer = AsyncMock(side_effect=fake_get_peer)
    fake.notify = AsyncMock()

    monkeypatch.setattr(messages, "get_peer_registry", lambda: fake)
    return fake


class TestInterruptForwardedToRegistry:
    async def test_interrupt_true_forwarded(self, monkeypatch):
        director = _make_peer("director-claude-code", PeerRole.ORCHESTRATOR)
        head = _make_peer("devops-head-claude-code", PeerRole.AGENT)
        fake = _make_fake_registry(monkeypatch, from_peer=director, to_peer=head)

        request = MagicMock()
        request.from_peer = director.display_name
        request.to_peer = head.display_name
        request.text = "[#notif-abcdef12] urgent"
        request.bypass_circle = False
        request.circle = None
        request.interrupt = True

        await messages.notify_peer(request, _=None)
        fake.notify.assert_awaited_once()
        kwargs = fake.notify.call_args.kwargs
        assert kwargs["interrupt"] is True

    async def test_interrupt_false_forwarded(self, monkeypatch):
        director = _make_peer("director-claude-code", PeerRole.ORCHESTRATOR)
        head = _make_peer("devops-head-claude-code", PeerRole.AGENT)
        fake = _make_fake_registry(monkeypatch, from_peer=director, to_peer=head)

        request = MagicMock()
        request.from_peer = director.display_name
        request.to_peer = head.display_name
        request.text = "regular message"
        request.bypass_circle = False
        request.circle = None
        request.interrupt = False

        await messages.notify_peer(request, _=None)
        kwargs = fake.notify.call_args.kwargs
        assert kwargs["interrupt"] is False


# interrupts.jsonl logging tests land with commit-4 (see test_interrupt_jsonl_log.py).
