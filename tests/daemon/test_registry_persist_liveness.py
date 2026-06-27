"""beads-jj7l — daemon-side persistent liveness + rehydrate-on-restart.

Root cause (s8di): ``PeerRegistry._peers`` (the only source of ``GET /peers``,
which the external no_peer_record watchdog reads) is wiped on every daemon
restart; only ``SessionMapping`` persists (identity, no liveness). A live
session reappears ONLY when its WS-hook reconnects and re-registers. Until
then it is mesh-invisible (``HasPeer=false``) and the watchdog reaps it as an
orphan at 60 min.

Fix (Variant C, hybrid): persist ``last_seen`` in ``SessionMapping`` and
rehydrate ``_peers`` from surviving mappings at startup as OFFLINE with a
fresh clock + a transient ``_rehydrated`` reuse-grace marker. Any record in
``GET /peers`` (even OFFLINE) → ``HasPeer=true`` → no false orphan reap. The
marker lets ``allocate_and_register`` reuse a rehydrated peer's identity on
reconnect regardless of the 120 s offline-reuse TTL, with no display-name
churn. Long-dead mappings are dropped on load by ``last_seen`` age (DoD#3).
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repowire.config.models import AgentType, Config
from repowire.daemon.peer_registry import PeerRegistry
from repowire.protocol.peers import PeerRole, PeerStatus


def _make_registry(tmp_path: Path, config: Config | None = None) -> PeerRegistry:
    return PeerRegistry(
        config=config or Config(),
        message_router=MagicMock(),
        persistence_path=tmp_path / "sessions.json",
    )


def _peer_path(tmp_path: Path, name: str) -> str:
    """A path that exists on disk — _load_mappings skips mappings whose path
    is gone, so a true-restart (second registry instance) test needs the dir
    to survive."""
    p = tmp_path / name
    p.mkdir(exist_ok=True)
    return str(p)


async def _register_and_persist(
    tmp_path: Path, path: str, role: PeerRole = PeerRole.AGENT, circle: str = "global"
) -> tuple[PeerRegistry, str, str]:
    """Register a peer, flush mappings to disk (simulates a daemon that ran)."""
    reg = _make_registry(tmp_path)
    pid, name = await reg.allocate_and_register(
        circle=circle, backend=AgentType.CLAUDE_CODE, path=path, role=role,
    )
    reg._mappings_dirty = True
    reg._persist_mappings()
    return reg, pid, name


# ---------------------------------------------------------------------------
# Persisting liveness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_mapping_persists_last_seen(tmp_path):
    """A persisted mapping must carry a parseable last_seen (real liveness)."""
    path = _peer_path(tmp_path, "director")
    _, pid, _ = await _register_and_persist(tmp_path, path, role=PeerRole.ORCHESTRATOR)

    data = json.loads((tmp_path / "sessions.json").read_text())
    assert pid in data
    assert data[pid].get("last_seen"), "last_seen must be persisted in sessions.json"
    _dt.datetime.fromisoformat(data[pid]["last_seen"])  # parseable ISO-8601


# ---------------------------------------------------------------------------
# Rehydration at startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrate_populates_peers_as_offline(tmp_path):
    """After a restart, rehydration puts surviving mappings back into _peers
    as OFFLINE (so GET /peers → HasPeer=true), marked _rehydrated, pane unbound."""
    path = _peer_path(tmp_path, "devops-worker")
    _, pid, _ = await _register_and_persist(tmp_path, path)

    reg_b = _make_registry(tmp_path)  # fresh process: loads mappings from disk
    assert reg_b._peers == {}, "no live peers right after restart"

    reg_b.rehydrate_from_mappings()

    peers = await reg_b.get_all_peers()
    assert len(peers) == 1
    p = peers[0]
    assert p.peer_id == pid
    assert p.status == PeerStatus.OFFLINE
    assert p.metadata.get("_rehydrated") is True
    assert p.pane_id is None


@pytest.mark.asyncio
async def test_rehydrated_peer_reused_past_ttl_no_suffix(tmp_path):
    """A rehydrated peer reconnecting AFTER the 120 s offline-reuse TTL must
    still reuse its peer_id and name (grace marker), not mint a '-2' duplicate.

    Without the marker, sub-case A declines (age>TTL, non-singleton) and the
    fresh path defers takeover (offline_since fresh < 30 s) → '-2' suffix +
    new peer_id.
    """
    path = _peer_path(tmp_path, "backend-head")
    _, first_id, first_name = await _register_and_persist(
        tmp_path, path, role=PeerRole.ORCHESTRATOR
    )

    reg_b = _make_registry(tmp_path)
    reg_b.rehydrate_from_mappings()
    # Force sub-case A's age>TTL while offline_since stays fresh (rehydration).
    reg_b._peers[first_id].last_seen = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=300)
    )

    second_id, second_name = await reg_b.allocate_and_register(
        circle="global", backend=AgentType.CLAUDE_CODE, path=path,
        role=PeerRole.ORCHESTRATOR,
    )

    assert second_id == first_id, "rehydrated peer_id must be reused"
    assert second_name == first_name
    assert "-2-" not in second_name
    assert reg_b._peers[second_id].status == PeerStatus.ONLINE
    assert not reg_b._peers[second_id].metadata.get("_rehydrated"), (
        "marker must clear on reuse"
    )


@pytest.mark.asyncio
async def test_rehydrate_backward_compat_no_last_seen(tmp_path):
    """A legacy sessions.json without last_seen must load and rehydrate."""
    path = _peer_path(tmp_path, "legacy-peer")
    sid = "repow-global-legacy01"
    fresh = _dt.datetime.now(_dt.timezone.utc).isoformat()
    data = {
        sid: {
            "session_id": sid,
            "display_name": "legacy-peer-claude-code",
            "circle": "global",
            "backend": "claude-code",
            "path": path,
            "role": "agent",
            "updated_at": fresh,
        }
    }
    (tmp_path / "sessions.json").write_text(json.dumps(data))

    reg_b = _make_registry(tmp_path)
    reg_b.prune_offline(reg_b._config.daemon.prune_max_age_hours)
    reg_b.rehydrate_from_mappings()  # must not raise

    assert sid in reg_b._peers
    assert reg_b._peers[sid].status == PeerStatus.OFFLINE


# ---------------------------------------------------------------------------
# DoD#3 — distinguish "should reconnect" from "long dead"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrate_skips_long_dead_mappings(tmp_path):
    """A mapping last seen beyond prune_max_age is dropped on load, not rehydrated."""
    path = _peer_path(tmp_path, "old-worker")
    _, pid, _ = await _register_and_persist(tmp_path, path)

    sessions = tmp_path / "sessions.json"
    data = json.loads(sessions.read_text())
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=25)).isoformat()
    data[pid]["last_seen"] = old
    data[pid]["updated_at"] = old
    sessions.write_text(json.dumps(data))

    reg_b = _make_registry(tmp_path)
    reg_b.prune_offline(reg_b._config.daemon.prune_max_age_hours)
    reg_b.rehydrate_from_mappings()

    assert pid not in reg_b._peers
    assert pid not in reg_b._mappings


@pytest.mark.asyncio
async def test_activation_restart_rehydrates_all_without_last_seen(tmp_path):
    """The jj7l ACTIVATION restart: the old sessions.json has NO last_seen
    (the pre-jj7l daemon never wrote it) and updated_at may be stale. Every
    path-existing mapping must STILL be rehydrated as OFFLINE — NOT false-pruned
    by the unreliable updated_at.

    This is the safety of the activation window itself: a live session must not
    become an orphan on the very restart that ships jj7l. Risk asymmetry — a
    wrongly-kept dead peer self-heals (OFFLINE → zombie-offline 60 min); a
    wrongly-pruned live peer is an orphan (the bug jj7l fixes).
    """
    p_live = _peer_path(tmp_path, "live-worker")
    p_quiet = _peer_path(tmp_path, "quiet-head")
    stale = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=100)).isoformat()
    data = {
        "repow-global-live0001": {
            "session_id": "repow-global-live0001",
            "display_name": "live-worker-claude-code",
            "circle": "global", "backend": "claude-code", "path": p_live,
            "role": "agent", "updated_at": stale,  # stale, NO last_seen
        },
        "repow-global-quiet001": {
            "session_id": "repow-global-quiet001",
            "display_name": "quiet-head-claude-code",
            "circle": "global", "backend": "claude-code", "path": p_quiet,
            "role": "orchestrator", "updated_at": stale,  # stale, NO last_seen
        },
    }
    (tmp_path / "sessions.json").write_text(json.dumps(data))

    reg = _make_registry(tmp_path)
    reg.prune_offline(reg._config.daemon.prune_max_age_hours)  # must NOT prune
    reg.rehydrate_from_mappings()

    assert "repow-global-live0001" in reg._peers
    assert "repow-global-quiet001" in reg._peers
    assert reg._peers["repow-global-live0001"].status == PeerStatus.OFFLINE
    assert reg._peers["repow-global-quiet001"].status == PeerStatus.OFFLINE
    assert len(reg._mappings) == 2, "no mapping false-pruned at activation"


@pytest.mark.asyncio
async def test_rehydrated_dead_peer_evicted_after_max_age(tmp_path):
    """A rehydrated peer that never reconnects self-heals: once its clock
    passes max-age it is evicted by the normal stale-peer sweep (AGENT role)."""
    path = _peer_path(tmp_path, "dead-worker")
    _, pid, _ = await _register_and_persist(tmp_path, path, role=PeerRole.AGENT)

    reg_b = _make_registry(tmp_path)
    reg_b.rehydrate_from_mappings()
    assert pid in reg_b._peers

    reg_b._peers[pid].last_seen = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=25)
    )
    evicted = await reg_b._evict_stale_peers()

    assert evicted == 1
    assert pid not in reg_b._peers


# ---------------------------------------------------------------------------
# §6 edge cases (head checkpoint): takeover grace, repair, pane, service no-dup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrated_peer_has_fresh_offline_clock(tmp_path):
    """offline_since is restart-time → within the 30 s takeover grace, so a
    just-rehydrated peer cannot be name-reclaimed before it reconnects."""
    path = _peer_path(tmp_path, "svc-worker")
    _, pid, _ = await _register_and_persist(tmp_path, path, role=PeerRole.AGENT)

    reg_b = _make_registry(tmp_path)
    reg_b.rehydrate_from_mappings()

    p = reg_b._peers[pid]
    assert p.offline_since is not None
    age = (_dt.datetime.now(_dt.timezone.utc) - p.offline_since).total_seconds()
    assert age < 5, "offline_since must be reset to restart-time (fresh grace)"


@pytest.mark.asyncio
async def test_rehydrated_peer_survives_lazy_repair(tmp_path):
    """A fresh-clock rehydrated OFFLINE peer survives a maintenance sweep
    (not a ghost demote target, not yet stale)."""
    path = _peer_path(tmp_path, "lr-worker")
    _, pid, _ = await _register_and_persist(tmp_path, path)

    reg_b = _make_registry(tmp_path)
    reg_b.rehydrate_from_mappings()
    await reg_b.lazy_repair()

    assert pid in reg_b._peers
    assert reg_b._peers[pid].status == PeerStatus.OFFLINE


@pytest.mark.asyncio
async def test_rehydrated_peer_not_addressable_by_stale_pane(tmp_path):
    """pane_id is None until the real reconnect re-binds it — no mis-routing."""
    path = _peer_path(tmp_path, "pane-worker")
    _, pid, _ = await _register_and_persist(tmp_path, path)

    reg_b = _make_registry(tmp_path)
    reg_b.rehydrate_from_mappings()

    assert reg_b._peers[pid].pane_id is None
    assert await reg_b.get_peer_by_pane("%99") is None


@pytest.mark.asyncio
async def test_rehydrated_service_peer_reused_not_duplicated(tmp_path):
    """7ijt no-dup: a user-facing service peer rehydrates once and its
    reconnect reuses the same entry (no second namesake)."""
    path = _peer_path(tmp_path, "brain-admin")
    _, pid, name = await _register_and_persist(tmp_path, path, role=PeerRole.SERVICE)

    reg_b = _make_registry(tmp_path)
    reg_b.rehydrate_from_mappings()
    assert len(reg_b._peers) == 1

    sid2, name2 = await reg_b.allocate_and_register(
        circle="global", backend=AgentType.CLAUDE_CODE, path=path,
        role=PeerRole.SERVICE,
    )

    assert sid2 == pid
    assert name2 == name
    assert len(reg_b._peers) == 1, "service peer must not be duplicated on reconnect"
    assert reg_b._peers[pid].status == PeerStatus.ONLINE
