"""beads-61w: POST /notify with interrupt=True writes a line to interrupts.jsonl."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from repowire.daemon.routes import messages
from repowire.protocol.peers import PeerRole, PeerStatus


def _make_peer(display_name: str, role: PeerRole) -> MagicMock:
    p = MagicMock()
    p.display_name = display_name
    p.role = role
    p.status = PeerStatus.ONLINE
    p.peer_id = f"pid-{display_name}"
    return p


def _fake_registry(monkeypatch, from_peer, to_peer):
    async def fake_get_peer(name, circle=None):
        return {
            from_peer.display_name: from_peer,
            to_peer.display_name: to_peer,
        }.get(name)

    fake = MagicMock()
    fake.lazy_repair = AsyncMock()
    fake.get_peer = AsyncMock(side_effect=fake_get_peer)
    fake.notify = AsyncMock()
    monkeypatch.setattr(messages, "get_peer_registry", lambda: fake)
    return fake


@pytest.fixture
def interrupt_log_path(monkeypatch, tmp_path):
    path = tmp_path / "interrupts.jsonl"
    monkeypatch.setenv("REPOWIRE_INTERRUPT_LOG", str(path))
    return path


class TestInterruptJsonlLog:
    async def test_interrupt_true_writes_entry(self, monkeypatch, interrupt_log_path):
        director = _make_peer("director-claude-code", PeerRole.ORCHESTRATOR)
        head = _make_peer("devops-head-claude-code", PeerRole.AGENT)
        _fake_registry(monkeypatch, from_peer=director, to_peer=head)

        request = MagicMock()
        request.from_peer = director.display_name
        request.to_peer = head.display_name
        request.text = "[#notif-deadbeef] !URGENT deploy broke"
        request.bypass_circle = False
        request.circle = None
        request.interrupt = True

        await messages.notify_peer(request, _=None)

        assert interrupt_log_path.exists(), "interrupts.jsonl must be written"
        lines = interrupt_log_path.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["from_peer"] == director.display_name
        assert entry["to_peer"] == head.display_name
        assert entry["correlation_id"] == "notif-deadbeef"
        assert entry["text_prefix"].startswith("[#notif-deadbeef]")
        assert "ts" in entry and entry["ts"].endswith("+00:00") or entry["ts"].endswith("Z")

    async def test_interrupt_false_no_write(self, monkeypatch, interrupt_log_path):
        director = _make_peer("director-claude-code", PeerRole.ORCHESTRATOR)
        head = _make_peer("devops-head-claude-code", PeerRole.AGENT)
        _fake_registry(monkeypatch, from_peer=director, to_peer=head)

        request = MagicMock()
        request.from_peer = director.display_name
        request.to_peer = head.display_name
        request.text = "regular message, no URGENT"
        request.bypass_circle = False
        request.circle = None
        request.interrupt = False

        await messages.notify_peer(request, _=None)
        assert not interrupt_log_path.exists(), "default-path /notify must not touch jsonl"

    async def test_text_prefix_truncated_to_200_chars(self, monkeypatch, interrupt_log_path):
        director = _make_peer("director-claude-code", PeerRole.ORCHESTRATOR)
        head = _make_peer("devops-head-claude-code", PeerRole.AGENT)
        _fake_registry(monkeypatch, from_peer=director, to_peer=head)

        long_text = "[#notif-c0ffee00] " + ("A" * 500)
        request = MagicMock()
        request.from_peer = director.display_name
        request.to_peer = head.display_name
        request.text = long_text
        request.bypass_circle = False
        request.circle = None
        request.interrupt = True

        await messages.notify_peer(request, _=None)

        entry = json.loads(interrupt_log_path.read_text().splitlines()[0])
        assert len(entry["text_prefix"]) <= 200

    async def test_no_correlation_id_still_logs_with_null(
        self, monkeypatch, interrupt_log_path
    ):
        """interrupt=True with no [#notif-XXX] prefix — still logs, correlation_id=null."""
        director = _make_peer("director-claude-code", PeerRole.ORCHESTRATOR)
        head = _make_peer("devops-head-claude-code", PeerRole.AGENT)
        _fake_registry(monkeypatch, from_peer=director, to_peer=head)

        request = MagicMock()
        request.from_peer = director.display_name
        request.to_peer = head.display_name
        request.text = "cli-direct urgent message"
        request.bypass_circle = False
        request.circle = None
        request.interrupt = True

        await messages.notify_peer(request, _=None)
        entry = json.loads(interrupt_log_path.read_text().splitlines()[0])
        assert entry["correlation_id"] is None

    async def test_second_interrupt_appends_not_overwrites(
        self, monkeypatch, interrupt_log_path
    ):
        director = _make_peer("director-claude-code", PeerRole.ORCHESTRATOR)
        head = _make_peer("devops-head-claude-code", PeerRole.AGENT)
        _fake_registry(monkeypatch, from_peer=director, to_peer=head)

        async def _post(text):
            req = MagicMock()
            req.from_peer = director.display_name
            req.to_peer = head.display_name
            req.text = text
            req.bypass_circle = False
            req.circle = None
            req.interrupt = True
            await messages.notify_peer(req, _=None)

        await _post("[#notif-aaaaaaaa] one")
        await _post("[#notif-bbbbbbbb] two")

        lines = interrupt_log_path.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["correlation_id"] == "notif-aaaaaaaa"
        assert json.loads(lines[1])["correlation_id"] == "notif-bbbbbbbb"
