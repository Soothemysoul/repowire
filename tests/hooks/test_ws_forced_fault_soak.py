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

pytestmark = pytest.mark.asyncio


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
