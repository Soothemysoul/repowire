"""beads-evl: peer-side WS reconnect resilience + pane-warning (Fix A + Fix C).

Covers the unbounded reconnect loop, capped exp backoff + full jitter,
peek-only intentional-marker guard, the supervise() watchdog, and the
tmux pane-warning (no stdin injection).
"""
from __future__ import annotations

import asyncio
import json

import pytest

import repowire.hooks.websocket_hook as wh
from repowire.config.models import AgentType


@pytest.fixture(autouse=True)
def _reset_ws_module_state():
    """main()/supervise() mutate module globals (_expected_command via the
    `global` in main(), _warn_active via the pane-warn helpers). Reset them
    after each test so state never leaks into other suites (e.g.
    tests/test_hooks.py::TestIsPaneSafe, which reads _expected_command)."""
    yield
    wh._expected_command = None
    wh._warn_active = False
    # beads-wi7y: register-verify alert dedup flag is also a module global.
    wh._register_alert_sent = False


# --- Task 1: capped exponential backoff + full jitter -----------------------


def test_backoff_capped_and_jittered(monkeypatch):
    # full jitter: delay in [0, min(cap, base*2**attempt)]
    monkeypatch.setattr(wh.random, "uniform", lambda a, b: b)  # take upper bound
    assert wh._compute_backoff(attempt=0, cap=30.0, base=1.0) == 1.0
    assert wh._compute_backoff(attempt=3, cap=30.0, base=1.0) == 8.0
    assert wh._compute_backoff(attempt=10, cap=30.0, base=1.0) == 30.0  # capped


def test_backoff_lower_bound_is_zero(monkeypatch):
    monkeypatch.setattr(wh.random, "uniform", lambda a, b: a)  # take lower bound
    assert wh._compute_backoff(attempt=5, cap=30.0, base=1.0) == 0.0


# --- Task 3: peek-only intentional-marker helper ----------------------------


def test_marker_present_peek_does_not_unlink(tmp_path, monkeypatch):
    role_dir = tmp_path / "ops" / "devops-head"
    role_dir.mkdir(parents=True)
    marker = role_dir / ".shutdown-intentional"
    marker.write_text("")
    monkeypatch.setattr(wh, "_marker_dir", lambda role: role_dir)
    assert wh._marker_present("devops-head") is True
    assert marker.exists()  # peek-only — NOT consumed (gateway owns consumption)


def test_marker_present_stale_is_false(tmp_path, monkeypatch):
    import os
    import time

    role_dir = tmp_path / "ops" / "devops-head"
    role_dir.mkdir(parents=True)
    marker = role_dir / ".restart-intentional"
    marker.write_text("")
    old = time.time() - 400  # > 300s max-age
    os.utime(marker, (old, old))
    monkeypatch.setattr(wh, "_marker_dir", lambda role: role_dir)
    assert wh._marker_present("devops-head") is False


def test_marker_present_none_role_is_false():
    assert wh._marker_present(None) is False  # graceful degradation (Task 0)


# --- Task 5: Fix C — tmux pane-warning (no stdin injection) ------------------


def test_pane_warn_set_uses_display_message_not_send_keys(monkeypatch):
    monkeypatch.setattr(wh, "_warn_active", False)
    cmds = []
    monkeypatch.setattr(wh.subprocess, "run", lambda args, **k: cmds.append(args))
    wh._pane_warn_set("%1")
    flat = [" ".join(c) for c in cmds]
    assert any("display-message" in f for f in flat)
    assert all("send-keys" not in f for f in flat)   # NEVER stdin injection
    assert all("display-popup" not in f for f in flat)


def test_pane_warn_clear_resets_indicator(monkeypatch):
    monkeypatch.setattr(wh, "_warn_active", False)
    cmds = []
    monkeypatch.setattr(wh.subprocess, "run", lambda args, **k: cmds.append(args))
    wh._pane_warn_set("%1")
    cmds.clear()
    wh._pane_warn_clear("%1")
    flat = [" ".join(c) for c in cmds]
    assert any("select-pane" in f or "set-option" in f for f in flat)


def test_warn_threshold_constant_present():
    assert isinstance(wh._WARN_AFTER_ATTEMPTS, int) and wh._WARN_AFTER_ATTEMPTS >= 1


# --- Task 2: unbounded reconnect loop + pane-safety guard in main() ----------


@pytest.mark.asyncio
async def test_main_stops_reconnecting_when_pane_unsafe(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    # pane unsafe from the start → main must return 0 without infinite loop
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: False)
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "get_display_name", lambda: "devops-head-claude-code")
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    rc = await asyncio.wait_for(wh.main(), timeout=2.0)
    assert rc == 0


@pytest.mark.asyncio
async def test_main_retries_past_old_50_cap(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "get_display_name", lambda: "devops-head-claude-code")
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(wh, "_pane_warn_set", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_clear", lambda pane_id: None)
    attempts = {"n": 0}

    def _safe(pane_id):
        # stay safe for >50 connect failures, then go unsafe to end the test
        return attempts["n"] < 60

    monkeypatch.setattr(wh, "_is_pane_safe", _safe)

    class _Boom:
        async def __aenter__(self):
            attempts["n"] += 1
            raise OSError("connect refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wh.websockets, "connect", lambda *a, **k: _Boom())
    rc = await asyncio.wait_for(wh.main(), timeout=5.0)
    assert attempts["n"] >= 51  # proves we blew past the old max_attempts=50
    assert rc == 0


# --- Task 4: supervise() watchdog outer loop --------------------------------


def test_supervise_respawns_on_crash_when_safe(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "devops-head")
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_marker_present", lambda role: False)
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    calls = {"n": 0}

    def _fake_run(coro):
        coro.close()
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash")   # first run crashes
        return 0                          # second run exits clean → stop

    monkeypatch.setattr(wh.asyncio, "run", _fake_run)
    rc = wh.supervise()
    assert calls["n"] == 2  # respawned exactly once after the crash
    assert rc == 0


def test_supervise_no_respawn_on_intentional_marker(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "devops-head")
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_marker_present", lambda role: True)  # intentional!
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    calls = {"n": 0}

    def _fake_run(coro):
        coro.close()
        calls["n"] += 1
        raise RuntimeError("crash")

    monkeypatch.setattr(wh.asyncio, "run", _fake_run)
    rc = wh.supervise()
    assert calls["n"] == 1  # crashed once, marker present → NO respawn
    assert rc == 1


def test_supervise_no_respawn_when_pane_unsafe(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "devops-head")
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: False)  # Claude gone
    monkeypatch.setattr(wh, "_marker_present", lambda role: False)
    calls = {"n": 0}

    def _fake_run(coro):
        coro.close()
        calls["n"] += 1
        raise RuntimeError("crash")

    monkeypatch.setattr(wh.asyncio, "run", _fake_run)
    wh.supervise()
    assert calls["n"] == 1  # pane unsafe → no respawn


# =====================================================================
# beads-wi7y: register-verify after (re)connect
#
# Root cause (s8di): the daemon's in-memory `_peers` registry (the source
# GET /peers / the watchdog reads) is wiped on every daemon restart. A live
# session reappears ONLY if its WS-hook reconnects; the `connected` handshake
# is trusted with no read-back. If re-registration silently never lands, the
# scope stays mesh-invisible (PeerLastSeenEpoch==0) until the 60-min reaper.
#
# wi7y adds an active post-connect verify: confirm the peer is really in
# GET /peers/by-pane; on DEFINITE absence → forced reconnect + one-shot alert;
# on a TRANSIENT fetch error → soft (never drop a healthy connection, §8).
# =====================================================================


# --- resolveSupervisor port (pure) ------------------------------------------


def test_resolve_supervisor_worker_maps_to_head():
    assert wh._resolve_supervisor("backend-worker") == "backend-head"
    assert wh._resolve_supervisor("devops-worker") == "devops-head"


def test_resolve_supervisor_head_and_pm_map_to_director():
    assert wh._resolve_supervisor("backend-head") == "director"
    assert wh._resolve_supervisor("pm") == "director"
    assert wh._resolve_supervisor("brain-admin") == "director"
    assert wh._resolve_supervisor("librarian") == "director"
    assert wh._resolve_supervisor("project-init") == "director"


def test_resolve_supervisor_gsd_dev_maps_to_pm():
    assert wh._resolve_supervisor("gsd-dev") == "pm"


def test_resolve_supervisor_director_and_empty_are_none():
    assert wh._resolve_supervisor("director") is None
    assert wh._resolve_supervisor(None) is None
    assert wh._resolve_supervisor("") is None


# --- _verify_registration tri-state (httpx mocked via a self-contained fake) -


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _install_fake_get(monkeypatch, responses):
    """Patch wh.httpx.AsyncClient with a fake whose .get() returns the queued
    responses in order. Returns a list that records each request's (url, headers).
    No pytest-httpx dependency."""
    requests: list[dict] = []
    queue = list(responses)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            requests.append({"url": url, "headers": headers or {}})
            return queue.pop(0)

    monkeypatch.setattr(wh.httpx, "AsyncClient", _Client)
    return requests


@pytest.fixture(autouse=True)
def _stable_daemon_target(monkeypatch):
    """conftest points the daemon at unroutable :1; pin a stable host:port and
    clear the auth token so the verify-GET URL/headers are predictable."""
    monkeypatch.setenv("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("REPOWIRE_DAEMON_PORT", "8377")
    monkeypatch.delenv("REPOWIRE_AUTH_TOKEN", raising=False)


_VERIFY_URL = "http://127.0.0.1:8377/peers/by-pane/%251"  # pane "%1" url-quoted


async def test_verify_present_returns_true(monkeypatch):
    reqs = _install_fake_get(monkeypatch, [_FakeResp(200, {"peer_id": "peer-1"})])
    assert await wh._verify_registration("%1", "peer-1") is True
    assert reqs[0]["url"] == _VERIFY_URL


async def test_verify_peer_id_mismatch_returns_false(monkeypatch):
    # 200 but a DIFFERENT peer holds this pane → definite absence of *us*
    # (original + one immediate retry both mismatch).
    _install_fake_get(
        monkeypatch,
        [_FakeResp(200, {"peer_id": "someone-else"}), _FakeResp(200, {"peer_id": "someone-else"})],
    )
    assert await wh._verify_registration("%1", "peer-1") is False


async def test_verify_404_returns_false(monkeypatch):
    _install_fake_get(monkeypatch, [_FakeResp(404), _FakeResp(404)])
    assert await wh._verify_registration("%1", "peer-1") is False


async def test_verify_5xx_returns_none_transient(monkeypatch):
    _install_fake_get(monkeypatch, [_FakeResp(503)])
    assert await wh._verify_registration("%1", "peer-1") is None


async def test_verify_network_error_returns_none_transient(monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise OSError("connect refused")

    monkeypatch.setattr(wh.httpx, "AsyncClient", _BoomClient)
    assert await wh._verify_registration("%1", "peer-1") is None


async def test_verify_retries_once_on_absence(monkeypatch):
    # First GET: absent (404). Immediate retry: present → True (micro-race absorbed).
    reqs = _install_fake_get(
        monkeypatch, [_FakeResp(404), _FakeResp(200, {"peer_id": "peer-1"})]
    )
    assert await wh._verify_registration("%1", "peer-1") is True
    assert len(reqs) == 2  # proves exactly one retry happened


async def test_verify_passes_auth_token_header(monkeypatch):
    monkeypatch.setenv("REPOWIRE_AUTH_TOKEN", "sekret")
    reqs = _install_fake_get(monkeypatch, [_FakeResp(200, {"peer_id": "peer-1"})])
    assert await wh._verify_registration("%1", "peer-1") is True
    assert reqs[0]["headers"].get("Authorization") == "Bearer sekret"


# --- _emit_register_alert routing -------------------------------------------


async def test_register_alert_routes_supervisor_and_telegram(monkeypatch):
    posts = []

    async def _fake_post(path, body):
        posts.append((path, body["to_peer"]))

    monkeypatch.setattr(wh, "_daemon_post", _fake_post)
    await wh._emit_register_alert(
        my_name="backend-worker-claude-code", role="backend-worker", reason="r"
    )
    targets = [to for (_, to) in posts]
    assert "backend-head-claude-code" in targets  # supervisor mesh
    assert "telegram" in targets  # human channel always


async def test_register_alert_director_role_is_telegram_only(monkeypatch):
    posts = []

    async def _fake_post(path, body):
        posts.append(body["to_peer"])

    monkeypatch.setattr(wh, "_daemon_post", _fake_post)
    await wh._emit_register_alert(
        my_name="director-claude-code", role="director", reason="r"
    )
    assert posts == ["telegram"]  # no supervisor for director → human only


# --- _reconnect_loop integration: verify drives forced-reconnect/alert -------


class _FakeWS:
    """Minimal websockets client stand-in for the connected handshake + msg loop."""

    def __init__(self, handshake, messages=None):
        self._handshake = handshake
        self._messages = list(messages or [])
        self.iter_entered = False
        self.closed = False

    async def send(self, data):
        return None

    async def recv(self):
        return json.dumps(self._handshake)

    def __aiter__(self):
        self.iter_entered = True
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


class _FakeConnectCM:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *a):
        self._ws.closed = True
        return False


def _pane_safe_for(n):
    """_is_pane_safe stub: True for the first n calls (= n loop iterations)."""
    state = {"i": 0}

    def _f(pane_id):
        state["i"] += 1
        return state["i"] <= n

    return _f


@pytest.fixture
def _loop_env(monkeypatch):
    """Neutralize all real I/O the reconnect loop touches except the bits each
    test wants to observe (websockets.connect, _verify_registration, alert)."""
    monkeypatch.setattr(wh, "read_pane_runtime_metadata", lambda pane_id: {})
    monkeypatch.setattr(wh, "write_pane_runtime_metadata", lambda pane_id, md: None)
    monkeypatch.setattr(wh, "clear_pane_runtime_state", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_set", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_clear", lambda pane_id: None)
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: "backend-worker")
    monkeypatch.setattr(wh, "_get_loaded_epoch", lambda: "epoch-1")


_HANDSHAKE = {
    "type": "connected",
    "session_id": "peer-1",
    "display_name": "backend-worker-claude-code",
}


async def _run_loop():
    return await asyncio.wait_for(
        wh._reconnect_loop(
            "%1",
            "ws://x/ws",
            "backend-worker-claude-code",
            "project-x",
            AgentType.CLAUDE_CODE,
            "/tmp/x",
            0,
        ),
        timeout=5.0,
    )


async def test_verify_pass_enters_message_loop(monkeypatch, _loop_env):
    created = []

    def _connect(*a, **k):
        ws = _FakeWS(_HANDSHAKE)
        created.append(ws)
        return _FakeConnectCM(ws)

    monkeypatch.setattr(wh.websockets, "connect", _connect)
    monkeypatch.setattr(wh, "_is_pane_safe", _pane_safe_for(1))

    async def _verify_ok(pane_id, peer_id):
        return True

    monkeypatch.setattr(wh, "_verify_registration", _verify_ok)
    alerts = []

    async def _alert(**k):
        alerts.append(k)

    monkeypatch.setattr(wh, "_emit_register_alert", _alert)

    rc = await _run_loop()
    assert rc == 0
    assert created[0].iter_entered is True  # verify passed → message loop reached
    assert alerts == []


async def test_verify_absence_forces_reconnect_no_message_loop(monkeypatch, _loop_env):
    created = []

    def _connect(*a, **k):
        ws = _FakeWS(_HANDSHAKE)
        created.append(ws)
        return _FakeConnectCM(ws)

    monkeypatch.setattr(wh.websockets, "connect", _connect)
    monkeypatch.setattr(wh, "_is_pane_safe", _pane_safe_for(3))

    async def _verify_absent(pane_id, peer_id):
        return False

    monkeypatch.setattr(wh, "_verify_registration", _verify_absent)

    async def _noop_alert(**k):
        return None

    monkeypatch.setattr(wh, "_emit_register_alert", _noop_alert)

    await _run_loop()
    assert len(created) == 3  # each absence forced a fresh reconnect
    assert all(ws.iter_entered is False for ws in created)  # never entered msg loop


async def test_reconnect_path_also_verifies(monkeypatch, _loop_env):
    """CRITICAL (director): verify must run on RECONNECT, not only fresh spawn."""
    verify_calls = []

    def _connect(*a, **k):
        return _FakeConnectCM(_FakeWS(_HANDSHAKE))

    monkeypatch.setattr(wh.websockets, "connect", _connect)
    monkeypatch.setattr(wh, "_is_pane_safe", _pane_safe_for(3))

    async def _verify(pane_id, peer_id):
        verify_calls.append(peer_id)
        return True  # passes → message loop ends → loop reconnects → verifies again

    monkeypatch.setattr(wh, "_verify_registration", _verify)

    await _run_loop()
    assert len(verify_calls) == 3  # verified on every (re)connect, not just the first


async def test_verify_failure_alerts_once_after_threshold(monkeypatch, _loop_env):
    def _connect(*a, **k):
        return _FakeConnectCM(_FakeWS(_HANDSHAKE))

    monkeypatch.setattr(wh.websockets, "connect", _connect)
    monkeypatch.setattr(wh, "_is_pane_safe", _pane_safe_for(6))

    async def _verify_absent(pane_id, peer_id):
        return False

    monkeypatch.setattr(wh, "_verify_registration", _verify_absent)

    alerts = []

    async def _alert(**k):
        alerts.append(k)

    monkeypatch.setattr(wh, "_emit_register_alert", _alert)

    await _run_loop()
    assert len(alerts) == 1  # one-shot despite 6 consecutive verify failures
    assert alerts[0]["role"] == "backend-worker"


async def test_transient_verify_does_not_drop_connection(monkeypatch, _loop_env):
    """§8: a transient verify-fetch error must NOT force a reconnect or alert."""
    created = []

    def _connect(*a, **k):
        ws = _FakeWS(_HANDSHAKE)
        created.append(ws)
        return _FakeConnectCM(ws)

    monkeypatch.setattr(wh.websockets, "connect", _connect)
    monkeypatch.setattr(wh, "_is_pane_safe", _pane_safe_for(1))

    async def _verify_transient(pane_id, peer_id):
        return None

    monkeypatch.setattr(wh, "_verify_registration", _verify_transient)

    alerts = []

    async def _alert(**k):
        alerts.append(k)

    monkeypatch.setattr(wh, "_emit_register_alert", _alert)

    await _run_loop()
    assert created[0].iter_entered is True  # connection kept → message loop reached
    assert alerts == []  # transient never alerts


async def test_verify_success_resets_alert_dedup(monkeypatch, _loop_env):
    def _connect(*a, **k):
        return _FakeConnectCM(_FakeWS(_HANDSHAKE))

    monkeypatch.setattr(wh.websockets, "connect", _connect)
    monkeypatch.setattr(wh, "_is_pane_safe", _pane_safe_for(7))

    # False x3 → alert#1; True → reset; False x3 → alert#2.
    seq = iter([False, False, False, True, False, False, False])

    async def _verify(pane_id, peer_id):
        return next(seq)

    monkeypatch.setattr(wh, "_verify_registration", _verify)

    alerts = []

    async def _alert(**k):
        alerts.append(k)

    monkeypatch.setattr(wh, "_emit_register_alert", _alert)

    await _run_loop()
    assert len(alerts) == 2  # success between two failure episodes re-armed the alert
