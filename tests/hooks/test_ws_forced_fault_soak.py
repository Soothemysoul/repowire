"""beads-63mm: literal forced-fault soak verification of the beads-evl fix.

Independent qa run proving the WS-peer-reconnect fix (PR #26, merge c841069)
survives *literal* network faults across 5 scenarios, each on a fully isolated
ephemeral uvicorn daemon — never the live ``repowire.service`` on ``:8377``
nor the live ``agent-gateway`` marker dirs under ``~/ai-infra/ops/``.

Scenarios 1 & 2 are real wall-clock soaks (``@pytest.mark.soak``, CI skips them
via ``-m "not soak"``). Scenarios 3-5 run in CI.

HARD safety invariants (see plan §"HARD safety invariants"):
  * Every daemon is our own uvicorn on a ``_free_port`` — asserted ``!= 8377``.
  * iptables rule is scoped to the ephemeral port, reverted in finally, and the
    pre/post ``iptables-save`` snapshots are asserted equal (no leaked rule).
  * Marker dir is monkeypatched to ``tmp_path`` + a fake role — the live
    ``~/ai-infra/ops/`` is never written.
  * tmux uses a unique throwaway ``ff-soak-<pid>`` session, killed in finally.

NOTE on backoff compression: ``_RECONNECT_CAP_SEC`` and ``_compute_backoff``'s
default ``cap`` are bound at *import* time (websocket_hook.py), so setting
``REPOWIRE_WS_RECONNECT_CAP_SEC`` after the module is imported is INEFFECTIVE.
Scenarios that need a compressed/forced backoff therefore monkeypatch
``wh._compute_backoff`` directly (same technique as the existing
``test_ws_reconnect_forced_fault.py``). Scenario 1 must NOT compress — default
backoff is the whole point — so it leaves ``_compute_backoff`` untouched.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import threading
import time

import httpx
import pytest

import repowire.hooks.websocket_hook as wh
from repowire.daemon.app import create_test_app

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` — this module mixes
# async scenario tests (1-4) with SYNC marker-guard tests (5, supervise() is
# sync). asyncio_mode="auto" (pyproject) auto-runs the async ones; a blanket
# asyncio marker would wrongly coerce the sync tests into coroutines.


@pytest.fixture(autouse=True)
def _reset_ws_module_state():
    """main()/supervise() set module globals; reset them so they never leak
    into other suites (tests/test_hooks.py reads _expected_command)."""
    yield
    wh._expected_command = None
    wh._warn_active = False


# --- shared real-socket helpers (copied from test_ws_reconnect_forced_fault) --


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _LiveDaemon:
    """Real uvicorn server in a background thread on a fixed port. ``stop()``
    closes the sockets — the kill the tests need. A second instance on the same
    port + persistence path simulates a daemon restart (identity reuse)."""

    def __init__(self, app, port: int) -> None:
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        deadline = time.time() + timeout
        while not self._server.started:
            if time.time() > deadline:
                raise TimeoutError("uvicorn did not start in time")
            time.sleep(0.02)

    def stop(self, timeout: float = 10.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=timeout)


async def _online_peer_id(port: int, timeout: float = 8.0) -> str:
    """Poll GET /peers until at least one ONLINE peer exists; return its id."""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get("/peers", timeout=2.0)
                peers = resp.json().get("peers", [])
                online = [p for p in peers if p.get("status") == "online"]
                if online:
                    return online[0]["peer_id"]
            except (httpx.HTTPError, KeyError):
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError("no online peer registered in time")


async def _online_peer_ids(port: int) -> dict[str, float]:
    """Single GET /peers snapshot → {peer_id: loop_time} for online peers."""
    now = asyncio.get_event_loop().time()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        try:
            resp = await client.get("/peers", timeout=2.0)
            peers = resp.json().get("peers", [])
        except (httpx.HTTPError, KeyError):
            return {}
    return {p["peer_id"]: now for p in peers if p.get("status") == "online"}


def _base_env(monkeypatch, port: int, name: str, agent_path) -> None:
    monkeypatch.setenv("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("REPOWIRE_DAEMON_PORT", str(port))
    monkeypatch.setenv("REPOWIRE_CIRCLE", "default")
    monkeypatch.setenv("REPOWIRE_DISPLAY_NAME", name)
    monkeypatch.setenv("REPOWIRE_AGENT_PATH", str(agent_path))
    monkeypatch.setenv("TMUX_PANE", "%1")


# === Scenario 1: literal >250s daemon-down on DEFAULT backoff ================


@pytest.mark.soak
async def test_default_backoff_survives_250s_outage(tmp_path, monkeypatch):
    """Real >250s daemon outage on the DEFAULT backoff (cap 30s, full jitter).

    The old 50-attempt cap exhausted inside ~100-250s under the old 2-5s
    backoff, so the hook would have been dead by ~250s. With the unbounded fix
    the hook keeps issuing real connect attempts past 250s and re-registers
    with the SAME peer_id when the daemon returns on the same port + persistence.
    """
    port = _free_port()
    assert str(port) != "8377", "must use an ephemeral port, never the live daemon"
    persistence = tmp_path / "sessions.json"
    agent_path = tmp_path / "soak-peer"
    agent_path.mkdir()

    _base_env(monkeypatch, port, "soak-peer", agent_path)
    # DEFAULT backoff — do NOT patch _compute_backoff (that is the whole point).
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "_pane_warn_set", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_clear", lambda pane_id: None)

    real_connect = wh.websockets.connect
    attempts = {"n": 0, "last_ts": 0.0}

    def _counting_connect(*a, **k):
        attempts["n"] += 1
        attempts["last_ts"] = asyncio.get_event_loop().time()
        return real_connect(*a, **k)

    monkeypatch.setattr(wh.websockets, "connect", _counting_connect)

    app1 = create_test_app(persistence_path=persistence)
    daemon1 = _LiveDaemon(app1, port)
    daemon1.start()
    hook_task = asyncio.create_task(wh.main())
    try:
        peer_id_1 = await _online_peer_id(port)
        app1.state.peer_registry._persist_mappings()
        assert persistence.exists(), "session mapping not persisted pre-kill"

        daemon1.stop()  # real socket close
        outage_start = asyncio.get_event_loop().time()

        # Wait until a connect attempt is observed PAST the old ~250s window.
        # The default backoff ceiling is 30s, so a live hook issues an attempt
        # at least every 30s — a fresh attempt past 250s is guaranteed within
        # ~280s. The hard cap only trips if the hook genuinely STOPPED (the
        # regression we're hunting). Avoids the boundary flakiness of asserting
        # a fixed-window last-timestamp.
        target = 250.0
        hard_cap = 330.0
        while attempts["last_ts"] - outage_start <= target:
            if asyncio.get_event_loop().time() - outage_start > hard_cap:
                break
            await asyncio.sleep(1.0)
        assert attempts["last_ts"] > outage_start + target, (
            "hook stopped issuing connects inside the >250s window — "
            "did the unbounded fix regress to a finite cap?"
        )

        # Recover on the SAME port + persistence → peer_id reuse.
        daemon2 = _LiveDaemon(create_test_app(persistence_path=persistence), port)
        daemon2.start()
        try:
            peer_id_2 = await _online_peer_id(port, timeout=60.0)  # <= cap+slack
            assert peer_id_2 == peer_id_1, (
                f"peer_id changed across restart: {peer_id_1} -> {peer_id_2}"
            )
        finally:
            daemon2.stop()
    finally:
        hook_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hook_task


# === Scenario 2: literal iptables-drop + recover ============================


def _iptables_save() -> str:
    """Snapshot only the actual RULE lines (``-A``/``-I``/``-D`` ...), in order.

    Raw ``iptables-save`` output embeds a timestamp comment and per-chain
    packet/byte counters (``:INPUT ACCEPT [pkts:bytes]``) that change on every
    call on a busy host — comparing the raw text would false-positive a "leak".
    The rule lines are what actually matter for "did we leak a DROP rule".
    """
    out = subprocess.run(
        ["sudo", "iptables-save"], capture_output=True, text=True, check=True
    ).stdout
    return "\n".join(line for line in out.splitlines() if line.startswith("-"))


def _iptables_drop(port: int) -> None:
    assert str(port) != "8377", "refusing to firewall the live daemon port"
    subprocess.run(
        ["sudo", "iptables", "-I", "INPUT", "1", "-p", "tcp",
         "--dport", str(port), "-j", "DROP"],
        check=True,
    )


def _iptables_revert(port: int) -> None:
    """Delete by full spec; loop until the rule is gone (idempotent teardown)."""
    assert str(port) != "8377"
    for _ in range(10):
        r = subprocess.run(
            ["sudo", "iptables", "-D", "INPUT", "-p", "tcp",
             "--dport", str(port), "-j", "DROP"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            break  # no more matching rules


@pytest.mark.soak
async def test_iptables_drop_then_recover(tmp_path, monkeypatch):
    """A real packet-level connection drop (not a mocked WS-close) interrupts the
    hook and is recovered from once the rule is removed. The DROP is scoped to
    the ephemeral port, reverted in finally, and the pre/post iptables-save
    snapshots are asserted equal (no leaked rule)."""
    port = _free_port()
    assert str(port) != "8377"
    persistence = tmp_path / "sessions.json"
    agent_path = tmp_path / "ipt-peer"
    agent_path.mkdir()

    _base_env(monkeypatch, port, "ipt-peer", agent_path)
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "_pane_warn_set", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_clear", lambda pane_id: None)
    # Compress backoff so recovery after revert is quick. Env-var is bound at
    # import time → ineffective; patch _compute_backoff directly (cap ~1s).
    real_backoff = wh._compute_backoff
    monkeypatch.setattr(
        wh, "_compute_backoff", lambda attempt, *a, **k: real_backoff(attempt, cap=1.0)
    )

    real_connect = wh.websockets.connect
    attempts = {"n": 0}

    def _counting_connect(*a, **k):
        attempts["n"] += 1
        return real_connect(*a, **k)

    monkeypatch.setattr(wh.websockets, "connect", _counting_connect)

    app = create_test_app(persistence_path=persistence)
    daemon = _LiveDaemon(app, port)
    daemon.start()
    hook_task = asyncio.create_task(wh.main())
    pre = _iptables_save()
    try:
        peer_id_1 = await _online_peer_id(port)
        app.state.peer_registry._persist_mappings()
        # Production faithfulness: session_handler ALWAYS spawns the ws-hook with
        # REPOWIRE_PEER_ID set (session_handler.py:268), so a reconnect to the
        # SAME running daemon takes the peer_id over in-place (peer_registry
        # allocate_and_register, peer_id branch) — deterministic regardless of
        # how fast the daemon marks the old half-open peer OFFLINE. Without it
        # the test races the daemon's ping_timeout offline-detection. main()
        # re-reads REPOWIRE_PEER_ID each reconnect (websocket_hook.py:666).
        monkeypatch.setenv("REPOWIRE_PEER_ID", peer_id_1)
        baseline = attempts["n"]

        _iptables_drop(port)
        try:
            # While packets are dropped the established WS breaks (client/daemon
            # ping timeout) and reconnect SYNs are silently dropped → the hook
            # keeps issuing fresh connect attempts that cannot complete. Prove
            # the drop actually interrupted by waiting for attempt growth.
            deadline = asyncio.get_event_loop().time() + 45.0
            while attempts["n"] <= baseline:
                if asyncio.get_event_loop().time() > deadline:
                    break
                await asyncio.sleep(0.5)
            assert attempts["n"] > baseline, (
                "no reconnect attempts during the iptables DROP — "
                "did the drop not interrupt the connection?"
            )
        finally:
            _iptables_revert(port)

        # After revert: the hook reconnects on the same port, same peer_id.
        peer_id_2 = await _online_peer_id(port, timeout=30.0)
        assert peer_id_2 == peer_id_1, (
            f"peer_id changed across the drop: {peer_id_1} -> {peer_id_2}"
        )
    finally:
        hook_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hook_task
        _iptables_revert(port)  # belt: ensure reverted on any path
        daemon.stop()
        post = _iptables_save()
        assert post == pre, "iptables ruleset leaked — pre/post snapshot differ"


# === Scenario 3: reconnect-storm / no thundering-herd (full jitter) =========


async def _capture_reconnect_times(port: int, expected: int, timeout: float) -> list[float]:
    """Poll GET /peers rapidly; record the loop-time each peer_id first appears
    online. Returns the list of first-online times (len == expected on success)."""
    first_seen: dict[str, float] = {}
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        while asyncio.get_event_loop().time() < deadline and len(first_seen) < expected:
            now = asyncio.get_event_loop().time()
            try:
                resp = await client.get("/peers", timeout=2.0)
                peers = resp.json().get("peers", [])
            except (httpx.HTTPError, KeyError):
                peers = []
            for p in peers:
                if p.get("status") == "online" and p["peer_id"] not in first_seen:
                    first_seen[p["peer_id"]] = now
            await asyncio.sleep(0.02)
    return list(first_seen.values())


async def test_reconnect_storm_is_jittered(tmp_path, monkeypatch):
    """When N peers reconnect simultaneously after the daemon returns,
    _compute_backoff's full jitter spreads their reconnects over time — no
    synchronized spike (anti thundering-herd). Run N real hooks in-process with
    per-task identity via contextvars (env is process-global, so it cannot carry
    N distinct identities concurrently)."""
    import contextvars

    port = _free_port()
    assert str(port) != "8377"
    persistence = tmp_path / "sessions.json"

    monkeypatch.setenv("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("REPOWIRE_DAEMON_PORT", str(port))
    monkeypatch.setenv("REPOWIRE_CIRCLE", "default")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "_pane_warn_set", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_clear", lambda pane_id: None)
    # Preserve full jitter, compress the ceiling so the spread is observable in
    # a CI-friendly window: uniform(0, min(4, 2**attempt)).
    real_backoff = wh._compute_backoff
    monkeypatch.setattr(
        wh, "_compute_backoff", lambda attempt, *a, **k: real_backoff(attempt, cap=4.0)
    )

    # Per-task identity: main() reads get_display_name()/resolve_agent_path()
    # (bare names in wh globals) → redirect them to a contextvar set per task.
    _ident: contextvars.ContextVar = contextvars.ContextVar("ident")
    monkeypatch.setattr(wh, "get_display_name", lambda: _ident.get()["name"])
    monkeypatch.setattr(wh, "resolve_agent_path", lambda: _ident.get()["path"])

    n_peers = 8

    async def _run_one(i: int):
        p = tmp_path / f"peer{i}"
        p.mkdir(exist_ok=True)
        _ident.set({"name": f"jitter-peer-{i}", "path": str(p)})
        return await wh.main()

    daemon = _LiveDaemon(create_test_app(persistence_path=persistence), port)
    daemon.start()
    tasks = [asyncio.create_task(_run_one(i)) for i in range(n_peers)]
    try:
        # 1. Bring all peers online.
        deadline = asyncio.get_event_loop().time() + 20.0
        while True:
            online = await _online_peer_ids(port)
            if len(online) >= n_peers:
                break
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"only {len(online)}/{n_peers} peers came online")
            await asyncio.sleep(0.1)

        # 2. Kill the daemon; let all N enter the reconnect loop (attempts climb
        #    so backoff ceilings reach the cap range).
        daemon.stop()
        await asyncio.sleep(2.0)

        # 3. Restart on the same port; capture per-peer first-online times.
        daemon2 = _LiveDaemon(create_test_app(persistence_path=persistence), port)
        daemon2.start()
        try:
            times = await _capture_reconnect_times(port, expected=n_peers, timeout=30.0)
            assert len(times) == n_peers, f"only {len(times)}/{n_peers} peers reconnected"
            spread = max(times) - min(times)
            assert spread > 0.5, (
                f"reconnects not jittered (spread={spread:.3f}s) — herd risk"
            )
        finally:
            daemon2.stop()
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


# === Scenario 4: Fix C pane indicator lifecycle (real tmux, stdin-safe) ======

_WARN_TITLE = "⚠ repowire WS lost"


@contextlib.contextmanager
def _tmux_throwaway_pane():
    """A unique detached throwaway session; never a live mesh pane. Killed in
    finally."""
    session = f"ff-soak-{os.getpid()}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "80", "-y", "24",
         "sleep", "100000"],
        check=True,
    )
    try:
        pane_id = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        yield session, pane_id
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


def _pane_title(pane_id: str) -> str:
    return subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_title}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _pane_capture(pane_id: str) -> str:
    return subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", pane_id],
        capture_output=True, text=True, check=True,
    ).stdout


async def _wait_until(predicate, timeout: float) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await asyncio.to_thread(predicate):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
async def test_fix_c_pane_indicator_lifecycle(tmp_path, monkeypatch):
    """Against a REAL throwaway tmux pane: the WS-lost indicator (pane title)
    appears only after the grace threshold (_WARN_AFTER_ATTEMPTS=3), clears on
    reconnect, and never injects into the pane buffer (stdin-safe — active turn
    unharmed). _pane_warn_set/_clear are NOT patched — we assert real tmux."""
    port = _free_port()
    assert str(port) != "8377"
    persistence = tmp_path / "sessions.json"
    agent_path = tmp_path / "fixc-peer"
    agent_path.mkdir()

    with _tmux_throwaway_pane() as (session, pane_id):
        _base_env(monkeypatch, port, "fixc-peer", agent_path)
        monkeypatch.setenv("TMUX_PANE", pane_id)
        # Reach attempt >= _WARN_AFTER_ATTEMPTS fast. Env-var is import-bound →
        # patch _compute_backoff directly to a small constant.
        monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.2)
        # Keep pane "safe" deterministically; do NOT patch _pane_warn_*.
        monkeypatch.setattr(wh, "_is_pane_safe", lambda pid: True)
        monkeypatch.setattr(wh, "_get_pane_command", lambda pid: "sleep")

        before = _pane_capture(pane_id)
        assert _pane_title(pane_id) != _WARN_TITLE

        app = create_test_app(persistence_path=persistence)
        daemon = _LiveDaemon(app, port)
        daemon.start()
        hook_task = asyncio.create_task(wh.main())
        try:
            await _online_peer_id(port)
            assert _pane_title(pane_id) != _WARN_TITLE  # no warn while healthy

            daemon.stop()  # trigger reconnect loop → after attempt>=3, warn
            assert await _wait_until(
                lambda: _pane_title(pane_id) == _WARN_TITLE, timeout=20.0
            ), "WS-lost indicator never appeared after the daemon died"

            # Recover → indicator cleared.
            daemon2 = _LiveDaemon(create_test_app(persistence_path=persistence), port)
            daemon2.start()
            try:
                await _online_peer_id(port, timeout=15.0)
                assert await _wait_until(
                    lambda: _pane_title(pane_id) == "", timeout=15.0
                ), "WS-lost indicator not cleared on reconnect"
            finally:
                daemon2.stop()

            # No stdin injection: the title/status-line warning must not have
            # added keystrokes/commands to the pane buffer.
            after = _pane_capture(pane_id)
            assert "repowire" not in after.replace(before, ""), (
                "Fix C must not inject into the pane buffer (stdin-safe)"
            )
        finally:
            hook_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hook_task


# === Scenario 5: intentional-marker guard (peek-only, no resurrection) =======
#
# HARD ISOLATION: the marker dir is monkeypatched to tmp_path and a fake
# non-real role ("forced-fault-fake-role") — the live ~/ai-infra/ops/ is NEVER
# touched, so agent-gateway can never see a test-written marker for a real peer.

_FAKE_ROLE = "forced-fault-fake-role"


def _isolate_marker_dir(tmp_path, monkeypatch):
    base = tmp_path / "ops" / _FAKE_ROLE
    base.mkdir(parents=True)
    monkeypatch.setattr(wh, "_marker_dir", lambda role: tmp_path / "ops" / role)
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: _FAKE_ROLE)
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    return base


def test_marker_guard_blocks_resurrection(tmp_path, monkeypatch):
    """A fresh .shutdown-intentional marker stops supervise() from re-entering
    main() (no peer resurrection), and the marker is peeked (stat), never
    unlinked — agent-gateway.monitor_loop must still see it."""
    base = _isolate_marker_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pid: True)  # only the marker may stop respawn

    marker = base / ".shutdown-intentional"
    marker.write_text("")  # fresh mtime → age < 300s

    calls = {"n": 0}

    async def _fake_main_async():
        calls["n"] += 1
        return 1  # crash-like return → would normally trigger respawn

    monkeypatch.setattr(wh, "main", _fake_main_async)

    rc = wh.supervise()
    assert calls["n"] == 1, "supervise re-entered main despite fresh marker (resurrection!)"
    assert rc == 1
    assert marker.exists(), "marker was consumed — must be peek-only (stat, no unlink)"


def test_marker_guard_absent_allows_respawn(tmp_path, monkeypatch):
    """With no marker present, supervise() DOES re-enter main() until the pane
    goes unsafe."""
    _isolate_marker_dir(tmp_path, monkeypatch)
    # No marker file. Pane safe for the first crash, unsafe after the 2nd main().
    safe_calls = {"n": 0}
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pid: safe_calls["n"] < 2)

    calls = {"n": 0}

    async def _fake_main_async():
        calls["n"] += 1
        safe_calls["n"] += 1
        return 1

    monkeypatch.setattr(wh, "main", _fake_main_async)

    rc = wh.supervise()
    assert calls["n"] >= 2, "supervise did not re-enter main when no marker present"
    assert rc == 1


def test_marker_guard_stale_marker_allows_respawn(tmp_path, monkeypatch):
    """A STALE marker (mtime older than the 300s freshness window) is treated as
    crash-after-write, not an intentional signal → supervise() still respawns."""
    base = _isolate_marker_dir(tmp_path, monkeypatch)
    safe_calls = {"n": 0}
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pid: safe_calls["n"] < 2)

    marker = base / ".shutdown-intentional"
    marker.write_text("")
    stale = time.time() - 400  # > _INTENTIONAL_MARKER_MAX_AGE_SEC (300)
    os.utime(marker, (stale, stale))

    calls = {"n": 0}

    async def _fake_main_async():
        calls["n"] += 1
        safe_calls["n"] += 1
        return 1

    monkeypatch.setattr(wh, "main", _fake_main_async)

    wh.supervise()
    assert calls["n"] >= 2, "stale marker wrongly honored — supervise did not respawn"
    assert marker.exists(), "stale marker must still not be consumed (peek-only)"
