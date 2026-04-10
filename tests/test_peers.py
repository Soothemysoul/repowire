"""Tests for repowire/protocol/peers.py — Peer model helpers."""

from datetime import datetime

from repowire.config.models import AgentType
from repowire.protocol.peers import Peer, PeerRole, PeerStatus


def _make_peer(**kwargs) -> Peer:
    defaults = dict(peer_id="repow-dev-a1b2c3d4", display_name="dev", path="/app", machine="laptop")
    defaults.update(kwargs)
    return Peer(**defaults)


class TestBackendHelpers:
    def test_is_claude_code(self):
        peer = _make_peer(backend=AgentType.CLAUDE_CODE)
        assert peer.is_claude_code()
        assert not peer.is_opencode()
        assert not peer.is_codex()
        assert not peer.is_gemini()

    def test_is_opencode(self):
        peer = _make_peer(backend=AgentType.OPENCODE)
        assert peer.is_opencode()
        assert not peer.is_claude_code()

    def test_is_codex(self):
        peer = _make_peer(backend=AgentType.CODEX)
        assert peer.is_codex()
        assert not peer.is_claude_code()

    def test_is_gemini(self):
        peer = _make_peer(backend=AgentType.GEMINI)
        assert peer.is_gemini()
        assert not peer.is_claude_code()

    def test_default_backend_is_claude_code(self):
        peer = _make_peer()
        assert peer.is_claude_code()


class TestPeerStatus:
    def test_default_status_is_offline(self):
        peer = _make_peer()
        assert peer.status == PeerStatus.OFFLINE

    def test_explicit_status(self):
        peer = _make_peer(status=PeerStatus.ONLINE)
        assert peer.status == PeerStatus.ONLINE

    def test_busy_status(self):
        peer = _make_peer(status=PeerStatus.BUSY)
        assert peer.status == PeerStatus.BUSY


class TestPeerRole:
    def test_default_role_is_agent(self):
        peer = _make_peer()
        assert peer.role == PeerRole.AGENT

    def test_agent_does_not_bypass_circles(self):
        peer = _make_peer(role=PeerRole.AGENT)
        assert not peer.bypasses_circles

    def test_service_bypasses_circles(self):
        peer = _make_peer(role=PeerRole.SERVICE)
        assert peer.bypasses_circles

    def test_orchestrator_bypasses_circles(self):
        peer = _make_peer(role=PeerRole.ORCHESTRATOR)
        assert peer.bypasses_circles

    def test_human_bypasses_circles(self):
        peer = _make_peer(role=PeerRole.HUMAN)
        assert peer.bypasses_circles


class TestToDict:
    def test_includes_all_fields(self):
        peer = _make_peer(description="doing stuff")
        d = peer.to_dict()
        assert d["peer_id"] == "repow-dev-a1b2c3d4"
        assert d["display_name"] == "dev"
        assert d["name"] == "dev"  # backward compat alias
        assert d["path"] == "/app"
        assert d["machine"] == "laptop"
        assert d["tmux_session"] is None
        assert d["backend"] == AgentType.CLAUDE_CODE
        assert d["description"] == "doing stuff"
        assert d["circle"] == "global"
        assert d["status"] == "offline"
        assert d["role"] == "agent"
        assert d["last_seen"] is None
        assert d["metadata"] == {}

    def test_status_serialized_as_string(self):
        peer = _make_peer(status=PeerStatus.ONLINE)
        assert peer.to_dict()["status"] == "online"

    def test_role_serialized_as_string(self):
        peer = _make_peer(role=PeerRole.ORCHESTRATOR)
        assert peer.to_dict()["role"] == "orchestrator"

    def test_last_seen_serialized_as_isoformat(self):
        ts = datetime(2025, 1, 15, 12, 0, 0)
        peer = _make_peer(last_seen=ts)
        assert peer.to_dict()["last_seen"] == ts.isoformat()

    def test_metadata_included(self):
        peer = _make_peer(metadata={"env": "prod", "version": "1.2"})
        assert peer.to_dict()["metadata"] == {"env": "prod", "version": "1.2"}


class TestLegacyFields:
    def test_name_maps_to_display_name(self):
        peer = Peer(name="legacy-peer", path="/app", machine="host")
        assert peer.display_name == "legacy-peer"
        assert peer.name == "legacy-peer"

    def test_peer_id_generated_from_name_when_missing(self):
        peer = Peer(name="legacy-peer", path="/app", machine="host")
        assert peer.peer_id == "legacy-legacy-peer"

    def test_peer_id_generated_from_tmux_session_priority(self):
        peer = Peer(
            display_name="x", path="/app", machine="host", tmux_session="dev:main"
        )
        # tmux_session has priority over display_name for legacy peer_id generation
        assert peer.peer_id == "legacy-dev:main"


class TestIsLocal:
    def test_local_when_tmux_session_set(self):
        peer = _make_peer(tmux_session="dev:claude")
        assert peer.is_local()

    def test_not_local_when_no_tmux_session(self):
        peer = _make_peer()
        assert not peer.is_local()
