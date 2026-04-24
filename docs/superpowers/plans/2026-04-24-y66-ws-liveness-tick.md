# beads-y66 — repowire WS liveness tick + client keepalive

**Branch:** `fix/y66-ws-liveness-tick`
**Base:** `origin/main` (repowire-fork)
**Task:** beads-y66 (P1)

## Problem in one sentence

`list_peers()` status drifts from actual deliverability: on silent TCP
death nobody notices (client has no WS keepalive, server's liveness
check is traffic-gated + 30s-debounced), so inbound `notify_peer` to
those peers 503s while status still reads online/offline-but-wrong.
Full investigation in `bd show beads-y66` (backend-head note dated
2026-04-24 ~15:05 MSK).

## Goal

Bring `peer.status` in sync with `transport.is_connected(peer_id)`
within 5 seconds of any WS state change — both directions.

## Non-goals

- Watchdog changes (brain-watchdog reading transport directly) —
  separate epic.
- Autorespawn on WS-flap-while-scope-alive — devops-head territory,
  separate epic.
- Graphify / wiki changes.

## Tasks

Each task is a small, grep-verifiable step. Follow the order. Commit
after each task with a descriptive message.

---

### Task 1 — Add `liveness_tick()` to `PeerRegistry`

**Read first:**
- `repowire/daemon/peer_registry.py` L1259-1314 (`_demote_disconnected_peers`, `_demote_unsafe_connected_peers`) — existing demotion logic to reuse.
- `repowire/daemon/peer_registry.py` L1241-1257 (`lazy_repair`) — shows how demotion is currently wired but traffic-gated.

**What to write:**

In `repowire/daemon/peer_registry.py`, add a new public method right
after `active_repair()` (around L1338). Do NOT touch existing
`lazy_repair` / `active_repair` — they stay as-is (callers on
endpoints still benefit).

```python
async def liveness_tick(self) -> None:
    """Reconcile peer.status with transport connection state.

    Cheap operation intended to run every ~5s from a background task.
    Unlike lazy_repair (30s-debounced, triggered on endpoints), this
    runs unconditionally — idle daemons still get accurate status.

    Logic:
    1. demote ghost peers whose status is ONLINE/BUSY but the
       transport has no WS for them — registry was stale.
    2. promote peers whose status is OFFLINE but the transport DOES
       have a live WS for them — usually after a race between an
       old handler's finally-demote and a new handler's connect.
    """
    transport = self._transport
    if not transport:
        return

    async with self._lock:
        ghosts = [
            p.peer_id for p in self._peers.values()
            if p.status in (PeerStatus.ONLINE, PeerStatus.BUSY)
            and not transport.is_connected(p.peer_id)
        ]
        resurrected = [
            p.peer_id for p in self._peers.values()
            if p.status == PeerStatus.OFFLINE
            and transport.is_connected(p.peer_id)
        ]

    for peer_id in ghosts:
        await self.mark_offline(peer_id)
    for peer_id in resurrected:
        await self.update_peer_status(peer_id, PeerStatus.ONLINE)

    if ghosts or resurrected:
        logger.info(
            "liveness_tick: demoted=%d promoted=%d",
            len(ghosts), len(resurrected),
        )
```

**Also add** (same file, after the method above):

```python
async def liveness_tick_loop(self, interval_sec: float = 5.0) -> None:
    """Background loop that calls liveness_tick every interval_sec.

    Cancelled via asyncio.CancelledError on daemon shutdown. Logs and
    swallows individual tick errors to keep the loop alive across a
    transient bug — the tick is best-effort reconciliation.
    """
    logger.info("liveness_tick_loop started (interval=%.1fs)", interval_sec)
    try:
        while True:
            try:
                await self.liveness_tick()
            except Exception:
                logger.exception("liveness_tick failed; continuing")
            await asyncio.sleep(interval_sec)
    except asyncio.CancelledError:
        logger.info("liveness_tick_loop cancelled")
        raise
```

**Verify:**
- `grep -n "async def liveness_tick" repowire/daemon/peer_registry.py` returns 2 hits (method + loop).
- `ruff check repowire/daemon/peer_registry.py` passes.
- `mypy` clean on the module (if project runs mypy).

Commit: `feat(daemon): liveness_tick method + loop for registry-transport sync (beads-y66)`

---

### Task 2 — Wire `liveness_tick_loop` into daemon lifespan

**Read first:**
- `repowire/daemon/app.py` L71-174 (`lifespan` context manager).

**What to change:**

In `lifespan()`, **after** `await peer_registry.start()` (currently L107), start the background task. **Before** the `yield` return in the shutdown path, cancel and await it.

Insert after L107 (`await peer_registry.start()`):

```python
        liveness_task = asyncio.create_task(
            peer_registry.liveness_tick_loop(),
            name="peer_registry.liveness_tick_loop",
        )
```

Replace the shutdown block (L165-173) with a version that cancels `liveness_task` before services teardown:

```python
        liveness_task.cancel()
        try:
            await liveness_task
        except asyncio.CancelledError:
            pass

        for name, svc in reversed(services):
            await svc.stop()  # type: ignore[union-attr]
            logger.info("%s service stopped", name)
        if relay_client:
            await relay_client.stop()
        peer_registry._save_events()
        peer_registry._persist_mappings()
        await peer_registry.stop()
        cleanup_deps()
```

**Verify:**
- `grep -n "liveness_tick_loop" repowire/daemon/app.py` returns 1 hit.
- `grep -n "liveness_task" repowire/daemon/app.py` returns ≥3 hits (create, cancel, await).
- Daemon starts without error: `uv run python -c "from repowire.daemon.app import create_app; create_app()"` exits 0 (no exception).

Commit: `feat(daemon): start liveness_tick_loop in lifespan + graceful cancel on shutdown (beads-y66)`

---

### Task 3 — Enable client-side WS keepalive

**Read first:**
- `repowire/hooks/websocket_hook.py` L526-600 (reconnect loop; specifically L531 `websockets.connect(uri, ping_interval=None, ping_timeout=None)`).

**What to change:**

Replace the `websockets.connect(...)` call on L531 with enabled keepalive:

```python
            async with websockets.connect(
                uri,
                ping_interval=20,
                ping_timeout=5,
            ) as websocket:
```

Do NOT change anything else in this file. The existing reconnect loop (L582-600) already handles `ConnectionClosed` / `WebSocketException` — once the websockets library raises on missed pong, the loop reconnects with the existing backoff.

**Rationale for 20s/5s:** small-enough to detect silent death within
~25s (before y66 Pattern C's observed ≥1h). Not so small that idle
chat sessions thrash the TCP connection. `ping_timeout=5` keeps the
MTU of a lost-network event short — user-visible recovery in under
30s combined with the server 5s tick.

**Verify:**
- `grep -n "ping_interval=20" repowire/hooks/websocket_hook.py` returns 1 hit.
- `grep -n "ping_interval=None" repowire/hooks/websocket_hook.py` returns 0 hits.

Commit: `fix(hook): enable WS ping_interval=20/timeout=5 to detect silent TCP death (beads-y66)`

---

### Task 4 — Regression test for liveness_tick

**Read first:**
- `repowire/daemon/websocket_transport.py` L30-109 (real transport API — we'll use a fake).
- `tests/daemon/test_peer_registry_reconnect.py` L1-30 (existing test fixture pattern `_make_registry`).

**What to write:**

Create new file `tests/daemon/test_ws_liveness_tick.py`:

```python
"""Regression tests for PeerRegistry.liveness_tick — beads-y66.

Guards the fix for registry.status drifting from transport connection
state. Pre-fix, Pattern C manifested as status=offline while
transport._connections had no WS (inbound notify 503), OR status=online
while transport was empty (stale).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repowire.config.models import AgentType, Config
from repowire.daemon.peer_registry import PeerRegistry
from repowire.protocol.peers import PeerRole, PeerStatus


class FakeTransport:
    """Minimal WebSocketTransport stand-in for liveness_tick tests.

    Exposes the methods liveness_tick actually calls: is_connected.
    ping() is not used by liveness_tick; include a stub only if the
    impl drifts to call it.
    """

    def __init__(self) -> None:
        self._connected: set[str] = set()

    def set_connected(self, peer_id: str, connected: bool) -> None:
        if connected:
            self._connected.add(peer_id)
        else:
            self._connected.discard(peer_id)

    def is_connected(self, peer_id: str) -> bool:
        return peer_id in self._connected


def _make_registry(tmp_path: Path, transport: FakeTransport) -> PeerRegistry:
    path = tmp_path / "sessions.json"
    return PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        transport=transport,  # type: ignore[arg-type]
        persistence_path=path,
    )


@pytest.mark.asyncio
async def test_liveness_tick_demotes_ghost_peer(tmp_path):
    """Peer ONLINE in registry but no WS in transport — tick marks OFFLINE.

    This is the classic Pattern C trigger: registry lying about deliverability.
    """
    transport = FakeTransport()
    registry = _make_registry(tmp_path, transport)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/ghost",
        role=PeerRole.AGENT,
    )
    # Initial status ONLINE per allocate_and_register contract
    assert registry._peers[peer_id].status == PeerStatus.ONLINE
    # Simulate transport without a WS (e.g. silent TCP death)
    assert not transport.is_connected(peer_id)

    await registry.liveness_tick()

    assert registry._peers[peer_id].status == PeerStatus.OFFLINE


@pytest.mark.asyncio
async def test_liveness_tick_promotes_resurrected_peer(tmp_path):
    """Peer OFFLINE in registry but live WS in transport — tick marks ONLINE.

    Race scenario: old handler's finally demoted the peer, new handler's
    connect set up transport but status flip lost to the race. Tick reconciles.
    """
    transport = FakeTransport()
    registry = _make_registry(tmp_path, transport)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/ghost",
        role=PeerRole.AGENT,
    )
    # Force OFFLINE (simulate stale demote)
    await registry.mark_offline(peer_id)
    assert registry._peers[peer_id].status == PeerStatus.OFFLINE
    # Simulate a live WS being in transport (new handler reconnect)
    transport.set_connected(peer_id, True)

    await registry.liveness_tick()

    assert registry._peers[peer_id].status == PeerStatus.ONLINE


@pytest.mark.asyncio
async def test_liveness_tick_is_idempotent_when_consistent(tmp_path):
    """Tick does nothing when registry and transport already agree."""
    transport = FakeTransport()
    registry = _make_registry(tmp_path, transport)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/consistent",
        role=PeerRole.AGENT,
    )
    transport.set_connected(peer_id, True)

    # Both sides agree: ONLINE + connected
    await registry.liveness_tick()
    assert registry._peers[peer_id].status == PeerStatus.ONLINE

    # Run twice — still stable
    await registry.liveness_tick()
    assert registry._peers[peer_id].status == PeerStatus.ONLINE


@pytest.mark.asyncio
async def test_liveness_tick_preserves_busy_when_connected(tmp_path):
    """A BUSY+connected peer stays BUSY after tick (not promoted to ONLINE)."""
    transport = FakeTransport()
    registry = _make_registry(tmp_path, transport)

    peer_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/tmp/busy",
        role=PeerRole.AGENT,
    )
    await registry.update_peer_status(peer_id, PeerStatus.BUSY)
    transport.set_connected(peer_id, True)

    await registry.liveness_tick()

    assert registry._peers[peer_id].status == PeerStatus.BUSY


@pytest.mark.asyncio
async def test_liveness_tick_no_transport_is_noop(tmp_path):
    """If transport is None (unit-test registry), tick is a silent no-op."""
    path = tmp_path / "sessions.json"
    registry = PeerRegistry(
        config=Config(),
        message_router=MagicMock(),
        transport=None,
        persistence_path=path,
    )
    # No crash, no exception
    await registry.liveness_tick()
```

**Verify:**
- `uv run pytest tests/daemon/test_ws_liveness_tick.py -v` → 5 passed.
- `uv run pytest tests/ -v --tb=short` → all existing tests still pass.
- `ruff check tests/daemon/test_ws_liveness_tick.py` clean.

Commit: `test(daemon): regression tests for liveness_tick reconciliation (beads-y66)`

---

### Task 5 — Final sanity sweep + CI

**What to do:**

1. From the worktree root, run full test suite:
   ```bash
   uv run pytest tests/ -q
   ```
   All tests must pass. If any pre-existing test breaks because of
   the tick promoting a previously-OFFLINE peer, check whether that
   test sets up a stale-registry scenario that the tick now
   (correctly) reconciles. Report any such cases to backend-head
   rather than editing them blind.

2. Lint:
   ```bash
   uv run ruff check repowire/ tests/
   ```

3. Push branch and open PR against `main`:
   ```bash
   git push -u origin fix/y66-ws-liveness-tick
   gh pr create --title "fix(daemon): liveness_tick + WS keepalive for inbound reconnect (beads-y66)" \
       --body "$(cat <<'EOF'
## Summary

Closes Pattern C from beads-y66: `list_peers().status` drifting from
`transport.is_connected(peer_id)`, causing inbound `notify_peer` 503s
to peers whose WS silently died.

- Server: new 5s background `liveness_tick` in PeerRegistry, wired via lifespan. Untethers reconciliation from traffic + 30s debounce of `lazy_repair`.
- Client: enable `ping_interval=20, ping_timeout=5` in websocket_hook — detects silent TCP death and triggers existing reconnect loop.
- Regression: 5 new tests in `tests/daemon/test_ws_liveness_tick.py`.

## Test plan

- [x] `uv run pytest tests/daemon/test_ws_liveness_tick.py -v` — 5 new tests pass
- [x] `uv run pytest tests/ -q` — full suite green
- [x] `uv run ruff check` clean
- [ ] (manual, post-merge) live WS disconnect on a head scope, `list_peers()` flips OFFLINE within 5s

## Out of scope

- Watchdog reading `transport.is_connected` directly — separate epic.
- Autorespawn on WS-flap-while-scope-alive — devops-head territory.

See beads-y66 notes for full investigation, including the live Pattern
C repro captured during the fix session itself.
EOF
)"
   ```

4. Post PR number + gh PR url to backend-head via `notify_peer`:
   ```
   notify_peer('backend-head-claude-code', 'y66 PR #<N> open on fix/y66-ws-liveness-tick, CI kicked off, link: <url>. Ready for review.')
   ```

5. When CI is green, wait for backend-head review. Do NOT self-merge.

Commit (for the PR description/branch-push only): no new file commit in this task.

---

## Assumptions pending user review

None. The approach is mechanical and the test contract matches the DoD word-for-word.

## Notes for the worker

- **Do NOT** add watchdog changes, autorespawn logic, or agent-gateway touches. That's out of scope — any scope creep gets the PR bounced.
- **Do NOT** remove or alter `lazy_repair` or `active_repair`. The 5s tick supplements them, not replaces.
- **Do NOT** modify `_demote_disconnected_peers` / `_demote_unsafe_connected_peers`. Reusing them by copy-logic in the new tick is intentional — they stay as endpoint-triggered safety nets.
- **Single-writer rule on this plan file**: head writes once, you read only. If anything is ambiguous mid-execution — notify_peer back to backend-head rather than editing the plan.
- Python version: repowire pins via `pyproject.toml` — use `uv run pytest` / `uv run ruff`.
- If `uv` commands fail because of environment drift, fall back to `.venv/bin/pytest`. But first notify backend-head.
