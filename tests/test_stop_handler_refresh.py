"""beads-rz1g parts 5/6/7: stop-hook acts on .refresh-pending at the turn boundary.

The Stop hook only fires BETWEEN turns, so a busy session always finishes its
current turn before any restart (NEVER kill mid-turn). On a fresh
``.refresh-pending`` marker the hook:
  - idempotency: a session already at target_epoch consumes the marker, no-op;
  - scope/role: orchestrators (director/pm) and scope=='advisory' never auto-
    restart (advisory — left to their own restart-overlay);
  - guard: an in-flight beads claim defers the refresh (marker left);
  - else: self-restart via detached `AGENT_RESTART=1 agent-stop` after a
    deterministic per-session jitter (thundering-herd mitigation).
"""

from __future__ import annotations

import time

import pytest

import repowire.hooks.stop_handler as sh


@pytest.fixture
def marker_dir_at(monkeypatch, tmp_path):
    """Route marker_dir at tmp and fix the role to a worker."""
    monkeypatch.setattr(sh, "marker_dir", lambda role: tmp_path / role)
    monkeypatch.setattr(sh, "resolve_agent_role", lambda: "backend-worker")
    return tmp_path


def _write_marker(tmp_path, role, payload, *, age_sec=0.0):
    import json

    d = tmp_path / role
    d.mkdir(parents=True, exist_ok=True)
    m = d / ".refresh-pending"
    m.write_text(json.dumps(payload))
    if age_sec:
        old = time.time() - age_sec
        import os

        os.utime(m, (old, old))
    return m


# --- part 7: jitter ---------------------------------------------------------


class TestJitter:
    def test_deterministic(self):
        assert sh._refresh_jitter("peer-abc") == sh._refresh_jitter("peer-abc")

    def test_in_range(self):
        for pid in ("a", "peer-xyz", "repow-global-80bbc633"):
            assert 0 <= sh._refresh_jitter(pid, window=30) < 30

    def test_distinct_peers_can_differ(self):
        vals = {sh._refresh_jitter(f"peer-{i}", window=30) for i in range(20)}
        assert len(vals) > 1  # not a constant

    def test_zero_window_is_zero(self):
        assert sh._refresh_jitter("x", window=0) == 0


# --- part 6: scope/role decision -------------------------------------------


class TestShouldSelfRestart:
    @pytest.mark.parametrize(
        "role,scope,expected",
        [
            ("backend-worker", "workers", True),
            ("backend-worker", "all", True),
            ("backend-head", "workers", False),  # heads not in 'workers' scope
            ("backend-head", "all", True),
            ("director", "all", False),  # orchestrator always advisory
            ("pm", "all", False),
            ("director", "workers", False),
            ("backend-worker", "advisory", False),
            ("backend-head", "advisory", False),
        ],
    )
    def test_matrix(self, role, scope, expected):
        assert sh._should_self_restart(role, scope) is expected


# --- part 5: orchestration at the turn boundary -----------------------------


class TestMaybeTriggerRefresh:
    @pytest.fixture(autouse=True)
    def _no_claim_no_meta(self, monkeypatch):
        monkeypatch.setattr(sh, "_has_inflight_claim", lambda: False)
        monkeypatch.setattr(sh, "_loaded_epoch_from_meta", lambda pane_id: None)

    @pytest.fixture
    def restarts(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sh, "_trigger_refresh_restart", lambda pane_id: calls.append(pane_id))
        return calls

    def test_no_marker_no_restart(self, marker_dir_at, restarts):
        sh._maybe_trigger_refresh("%1")
        assert restarts == []

    def test_worker_restarts_and_consumes_marker(self, marker_dir_at, restarts):
        m = _write_marker(marker_dir_at, "backend-worker",
                          {"target_epoch": "new", "reason": "deploy", "scope": "workers"})
        sh._maybe_trigger_refresh("%1")
        assert restarts == ["%1"]
        assert not m.exists()  # consumed before restart

    def test_idempotent_when_loaded_equals_target(self, marker_dir_at, restarts, monkeypatch):
        monkeypatch.setattr(sh, "_loaded_epoch_from_meta", lambda pane_id: "new")
        m = _write_marker(marker_dir_at, "backend-worker",
                          {"target_epoch": "new", "reason": "r", "scope": "workers"})
        sh._maybe_trigger_refresh("%1")
        assert restarts == []
        assert not m.exists()  # consumed (already fresh), no restart

    def test_inflight_claim_defers_and_keeps_marker(self, marker_dir_at, restarts, monkeypatch):
        monkeypatch.setattr(sh, "_has_inflight_claim", lambda: True)
        m = _write_marker(marker_dir_at, "backend-worker",
                          {"target_epoch": "new", "reason": "r", "scope": "workers"})
        sh._maybe_trigger_refresh("%1")
        assert restarts == []
        assert m.exists()  # left for the next turn boundary

    def test_advisory_scope_no_restart_marker_left(self, marker_dir_at, restarts):
        m = _write_marker(marker_dir_at, "backend-worker",
                          {"target_epoch": "new", "reason": "r", "scope": "advisory"})
        sh._maybe_trigger_refresh("%1")
        assert restarts == []
        assert m.exists()

    def test_orchestrator_no_restart(self, monkeypatch, tmp_path, restarts):
        monkeypatch.setattr(sh, "marker_dir", lambda role: tmp_path / role)
        monkeypatch.setattr(sh, "resolve_agent_role", lambda: "director")
        m = _write_marker(tmp_path, "director",
                          {"target_epoch": "new", "reason": "r", "scope": "all"})
        sh._maybe_trigger_refresh("%1")
        assert restarts == []
        assert m.exists()

    def test_stale_marker_consumed_no_restart(self, marker_dir_at, restarts):
        m = _write_marker(marker_dir_at, "backend-worker",
                          {"target_epoch": "new", "reason": "r", "scope": "workers"},
                          age_sec=sh._REFRESH_MARKER_MAX_AGE_SEC + 10)
        sh._maybe_trigger_refresh("%1")
        assert restarts == []
        assert not m.exists()  # stale → consumed, ignored


# --- guard: in-flight beads claim -------------------------------------------


class TestHasInflightClaim:
    def test_no_assignee_returns_false(self, monkeypatch):
        monkeypatch.delenv("BD_ASSIGNEE", raising=False)
        monkeypatch.delenv("REPOWIRE_DISPLAY_NAME", raising=False)
        assert sh._has_inflight_claim() is False

    def test_issues_present_returns_true(self, monkeypatch):
        monkeypatch.setenv("BD_ASSIGNEE", "me")
        monkeypatch.setattr(sh.subprocess, "run", lambda *a, **k: _Result(0, '[{"id":"beads-1"}]'))
        assert sh._has_inflight_claim() is True

    def test_no_issues_returns_false(self, monkeypatch):
        monkeypatch.setenv("BD_ASSIGNEE", "me")
        monkeypatch.setattr(sh.subprocess, "run", lambda *a, **k: _Result(0, "[]"))
        assert sh._has_inflight_claim() is False

    def test_bd_missing_returns_false(self, monkeypatch):
        monkeypatch.setenv("BD_ASSIGNEE", "me")

        def _raise(*a, **k):
            raise FileNotFoundError("bd")

        monkeypatch.setattr(sh.subprocess, "run", _raise)
        assert sh._has_inflight_claim() is False

    def test_bd_error_returns_true_failclosed(self, monkeypatch):
        monkeypatch.setenv("BD_ASSIGNEE", "me")
        monkeypatch.setattr(sh.subprocess, "run", lambda *a, **k: _Result(1, ""))
        assert sh._has_inflight_claim() is True


# --- restart trigger --------------------------------------------------------


class TestTriggerRefreshRestart:
    def test_no_scope_name_is_noop(self, monkeypatch):
        monkeypatch.delenv("SCOPE_NAME", raising=False)
        calls = []
        monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
        sh._trigger_refresh_restart("%1")
        assert calls == []

    def test_spawns_detached_agent_stop(self, monkeypatch):
        monkeypatch.setenv("SCOPE_NAME", "agent-backend-worker-1")
        monkeypatch.setenv("REPOWIRE_PEER_ID", "peer-1")
        monkeypatch.setattr(sh, "_refresh_jitter", lambda pid, window=30: 0)
        calls = []
        monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: calls.append(a[0]))
        sh._trigger_refresh_restart("%1")
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "setsid"
        assert "AGENT_RESTART=1 agent-stop" in cmd[-1]


class _Result:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
