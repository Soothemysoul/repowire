"""beads-evl: real socket-kill forced-fault — hook survives a daemon outage
longer than the old 50-attempt cap and re-registers on recovery.

This is NOT a mocked WS-drop: a real uvicorn daemon is started on a real TCP
port, the ws-hook ``main()`` connects to it over a real WebSocket, the daemon
process is then *killed* (server stopped → listening + client sockets closed),
and we prove the hook keeps issuing real ``websockets.connect`` attempts past
the old ``max_attempts=50`` cap. When the daemon comes back on the SAME port
with the SAME on-disk persistence, the hook reconnects and the peer is
re-registered with the SAME peer_id (identity reuse).

The backoff *delay* is compressed via a ``_compute_backoff`` monkeypatch so the
test proves "unbounded, beats the 50-cap" in well under a second instead of the
literal >250s wall-clock the old cap implied. Everything that fails is real: the
socket, the kill, the reconnect attempts.

The literal >250s iptables-drop soak on the default backoff is intentionally
out of scope here — it is an independent qa-head run (beads-63mm).
"""
from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest
import uvicorn

import repowire.hooks.websocket_hook as wh
from repowire.daemon.app import create_test_app

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_ws_module_state():
    """main() sets the _expected_command module global; reset it after the test
    so it never leaks into other suites (tests/test_hooks.py reads it)."""
    yield
    wh._expected_command = None
    wh._warn_active = False


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _LiveDaemon:
    """Run a real uvicorn server in a background thread on a fixed port.

    Each instance is a distinct daemon "process" from the hook's point of view
    (real listening socket). ``stop()`` closes the sockets — the kill the test
    needs. A second instance on the same port + persistence path simulates a
    daemon restart.
    """

    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        deadline = asyncio.get_event_loop().time() + timeout
        while not self._server.started:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("uvicorn did not start in time")
            # blocking sleep is fine — start() runs before the async polling
            import time as _t

            _t.sleep(0.02)

    def stop(self, timeout: float = 10.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=timeout)


async def _online_peer_id(port: int, timeout: float = 8.0) -> str:
    """Poll GET /peers until exactly one ONLINE peer exists; return its id."""
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


async def test_hook_reconnects_after_real_socket_kill(tmp_path, monkeypatch):
    port = _free_port()
    persistence = tmp_path / "sessions.json"
    # The identity path must exist on disk: _load_mappings drops mappings whose
    # path is gone (treated as a dead agent), which would defeat reuse on restart.
    agent_path = tmp_path / "forced-fault-peer"
    agent_path.mkdir()

    # ws-hook env: point at the live daemon, stable identity, tmux-free.
    monkeypatch.setenv("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("REPOWIRE_DAEMON_PORT", str(port))
    monkeypatch.setenv("REPOWIRE_CIRCLE", "default")
    monkeypatch.setenv("REPOWIRE_DISPLAY_NAME", "forced-fault-peer")
    monkeypatch.setenv("REPOWIRE_AGENT_PATH", str(agent_path))
    monkeypatch.setenv("TMUX_PANE", "%1")

    # Keep the pane "alive" without touching tmux; compress backoff; mute warns.
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.001)
    monkeypatch.setattr(wh, "_pane_warn_set", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_clear", lambda pane_id: None)

    # Count REAL connect attempts (wrapper delegates to the real coroutine fn).
    real_connect = wh.websockets.connect
    attempts = {"n": 0}

    def _counting_connect(*args, **kwargs):
        attempts["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(wh.websockets, "connect", _counting_connect)

    # 1. Start the real daemon and connect the hook.
    app1 = create_test_app(persistence_path=persistence)
    daemon1 = _LiveDaemon(app1, port)
    daemon1.start()
    hook_task = asyncio.create_task(wh.main())
    try:
        peer_id_1 = await _online_peer_id(port)

        # Force the session mapping to disk before the kill. lazy_repair (the
        # only persist trigger) is throttled to 1x/30s and stop() does NOT
        # flush, so a short-lived test daemon would otherwise never write
        # sessions.json — and identity reuse on restart reads it from disk.
        app1.state.peer_registry._persist_mappings()
        assert persistence.exists(), "session mapping was not persisted pre-kill"

        # 2. KILL the daemon (real socket close, not a mocked WS-drop).
        daemon1.stop()
        baseline = attempts["n"]

        # 3. While down, the hook must keep issuing real connect attempts well
        #    past the old 50-cap. ECONNREFUSED against the dead port is a real
        #    socket-level failure exercising the OSError reconnect branch.
        deadline = asyncio.get_event_loop().time() + 8.0
        while attempts["n"] < baseline + 51:
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.02)
        assert attempts["n"] >= baseline + 51, (
            f"hook stopped reconnecting at {attempts['n'] - baseline} attempts "
            "(old max_attempts=50 regression?)"
        )

        # 4. Restart the daemon on the SAME port + persistence → recovery.
        daemon2 = _LiveDaemon(create_test_app(persistence_path=persistence), port)
        daemon2.start()
        try:
            peer_id_2 = await _online_peer_id(port, timeout=10.0)
            # 5. Re-registered with the SAME peer_id (identity reuse on disk).
            assert peer_id_2 == peer_id_1, (
                f"peer_id changed across restart: {peer_id_1} -> {peer_id_2}"
            )
        finally:
            daemon2.stop()
    finally:
        hook_task.cancel()
        try:
            await hook_task
        except asyncio.CancelledError:
            pass
