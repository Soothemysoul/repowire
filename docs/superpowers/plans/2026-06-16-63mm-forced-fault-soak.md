# beads-63mm — Forced-fault WS-reconnect verification (literal soak) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for each task. Steps use checkbox (`- [ ]`) syntax for tracking. This is a **verification** plan — no production code changes; you write/extend tests only.

**Goal:** Empirically prove the beads-evl fix (PR #26, merge `c841069`) survives *literal* network faults across 5 scenarios, on a fully isolated ephemeral daemon — never touching the live `repowire.service` on `:8377` nor the live `agent-gateway` marker dirs.

**Architecture:** Mirror the `_LiveDaemon` / `_free_port` real-socket pattern from `tests/hooks/test_ws_reconnect_forced_fault.py`. Add a new module `tests/hooks/test_ws_forced_fault_soak.py` carrying the literal (uncompressed) scenarios, marked `@pytest.mark.soak` so CI skips them — this is an independent qa-head run. Scenarios that prove a *distribution* (jitter) or a *threshold* (Fix C grace) may compress backoff via `REPOWIRE_WS_RECONNECT_CAP_SEC`; scenario 1 must NOT compress (default backoff is the point).

**Tech Stack:** pytest, pytest-asyncio, uvicorn, httpx, websockets, real tmux, passwordless `sudo iptables`.

---

## HARD safety invariants (mesh-safety — read before writing any test)

These are non-negotiable. A violation can take down the live mesh (director / brain-admin / telegram) or kill a live peer. Director flagged both classes explicitly.

1. **Ephemeral daemon only.** Every daemon is your own `uvicorn` on a `_free_port()` port. Assert `port != 8377` at the top of every test. Never connect a hook to `:8377`.
2. **iptables scoped + reverted (scenario 2).**
   - Snapshot rules BEFORE: `pre = sudo iptables-save`.
   - Rule MUST be scoped to your ephemeral port only: `sudo iptables -I INPUT 1 -p tcp --dport <port> -j DROP` (insert at position 1 so it precedes any `-A INPUT -i lo -j ACCEPT`).
   - Teardown in a `try/finally` AND register an `atexit`/fixture-finalizer belt: `sudo iptables -D INPUT -p tcp --dport <port> -j DROP` (delete by full spec; loop-delete until absent).
   - Snapshot AFTER teardown: `post = sudo iptables-save`; **assert `post == pre`** (no leaked rule).
   - **assert no rule references `8377`** in the rule you add (you only ever pass your ephemeral `<port>`; add an explicit `assert str(port) != "8377"` guard).
3. **Marker isolation (scenario 5).** `wh._marker_dir(role)` is hardcoded to `$HOME/ai-infra/ops/<role>/` — there is **no env-override**. Therefore you MUST `monkeypatch.setattr(wh, "_marker_dir", lambda role: tmp_path / "ops" / role)` so the hook reads markers ONLY from `tmp_path`, never the live `~/ai-infra/ops/`. Belt-and-suspenders: also use a **fake non-real role name** (`"forced-fault-fake-role"`) so even an un-patched path can never collide with a real agent gateway manages. NEVER write `.shutdown-intentional` / `.restart-intentional` into a real role dir.
4. **tmux isolation (scenario 4).** Create a unique throwaway detached session (`tmux new-session -d -s ff-soak-$$-<n>`), operate only on its pane, kill it in `finally`. Never target a live mesh pane.
5. **Module-global reset.** `main()` sets `wh._expected_command` and toggles `wh._warn_active`. Reuse the `_reset_ws_module_state` autouse fixture pattern from the existing forced-fault test in the new module too.

---

## File Structure

- Create: `tests/hooks/test_ws_forced_fault_soak.py` — all 5 literal scenarios.
- Create (optional helper, only if shared by ≥2 tests): inline helpers in the same module (`_free_port`, `_LiveDaemon`, `_online_peer_id`, `_iptables_drop`/`_iptables_revert`, `_tmux_throwaway_pane`). Prefer copying `_free_port` / `_LiveDaemon` / `_online_peer_id` verbatim from `tests/hooks/test_ws_reconnect_forced_fault.py` (DRY across the test suite is secondary to keeping each forced-fault module self-contained and readable).
- Modify: `pyproject.toml` / `pytest.ini` / `setup.cfg` (whichever holds pytest config) — register the `soak` marker so `@pytest.mark.soak` does not warn and CI can `-m "not soak"`.
- Reference (do NOT modify): `repowire/hooks/websocket_hook.py`, `tests/hooks/test_ws_reconnect_forced_fault.py`.

---

## Task 0: Preflight — capability check + marker register

**Files:**
- Test: `tests/hooks/test_ws_forced_fault_soak.py`

- [ ] **Step 1: Confirm capabilities (run, record in PR description — not a test)**

Run:
```bash
sudo -n iptables -L -n >/dev/null 2>&1 && echo "iptables OK" || echo "iptables MISSING"
tmux -V
python -c "import uvicorn, httpx, websockets; print('deps OK')"
```
Expected: `iptables OK`, a tmux version, `deps OK`. If `iptables MISSING` → STOP, report to qa-head (head escalates). qa-head already confirmed passwordless sudo iptables on this host; if it regressed, scenario 2 is blocked.

- [ ] **Step 2: Register the `soak` marker**

Find the pytest config (`pyproject.toml [tool.pytest.ini_options].markers`, or `pytest.ini`/`setup.cfg`). Add:
```
markers =
    soak: long real-wall-clock forced-fault tests; excluded from CI via -m "not soak"
```
(Append to the existing `markers` list — do not drop existing markers. Read the file first.)

- [ ] **Step 3: Verify CI exclusion command works**

Run: `pytest tests/hooks/test_ws_forced_fault_soak.py -m "not soak" --collect-only -q`
Expected: collects only the non-soak scenarios (3, 4, 5 if you mark 1 & 2 as soak); no warnings about an unknown `soak` marker.

- [ ] **Step 4: Commit**
```bash
git add tests/hooks/test_ws_forced_fault_soak.py pyproject.toml
git commit -m "test(forced-fault): scaffold soak module + register soak marker (beads-63mm)"
```

---

## Task 1: Scenario 1 — literal >250s daemon-down on DEFAULT backoff (`@pytest.mark.soak`)

Proves: with the unbounded fix and DEFAULT backoff (cap 30s, NO `_compute_backoff` monkeypatch), the hook survives a daemon outage longer than the old 50-attempt *time window* (~100–250s under the old 2–5s backoff) and re-registers with peer_id reuse on recovery. This is the scenario the existing compressed test explicitly defers (see its docstring).

**Files:**
- Test: `tests/hooks/test_ws_forced_fault_soak.py::test_default_backoff_survives_250s_outage`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.soak
async def test_default_backoff_survives_250s_outage(tmp_path, monkeypatch):
    port = _free_port()
    assert str(port) != "8377", "must use an ephemeral port, never the live daemon"
    persistence = tmp_path / "sessions.json"
    agent_path = tmp_path / "soak-peer"
    agent_path.mkdir()

    monkeypatch.setenv("REPOWIRE_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("REPOWIRE_DAEMON_PORT", str(port))
    monkeypatch.setenv("REPOWIRE_CIRCLE", "default")
    monkeypatch.setenv("REPOWIRE_DISPLAY_NAME", "soak-peer")
    monkeypatch.setenv("REPOWIRE_AGENT_PATH", str(agent_path))
    monkeypatch.setenv("TMUX_PANE", "%1")
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
        assert persistence.exists()

        daemon1.stop()                       # real socket close
        outage_start = asyncio.get_event_loop().time()

        # Stay down >250s — the old time window. Prove the hook is STILL
        # attempting at the end (would have been dead by attempt 50 under the
        # old cap+backoff well inside this window).
        OUTAGE_SEC = 260.0
        while asyncio.get_event_loop().time() - outage_start < OUTAGE_SEC:
            await asyncio.sleep(1.0)
        assert attempts["last_ts"] > outage_start + 250.0, (
            "hook stopped issuing connects inside the >250s window — "
            "did the unbounded fix regress?"
        )

        # Recover on the SAME port + persistence.
        daemon2 = _LiveDaemon(create_test_app(persistence_path=persistence), port)
        daemon2.start()
        try:
            peer_id_2 = await _online_peer_id(port, timeout=60.0)  # ≤ cap+slack
            assert peer_id_2 == peer_id_1
        finally:
            daemon2.stop()
    finally:
        hook_task.cancel()
        try:
            await hook_task
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 2: Run it (will pass if fix is correct — this is verification, not red-first)**

Run: `pytest tests/hooks/test_ws_forced_fault_soak.py::test_default_backoff_survives_250s_outage -m soak -v`
Expected: PASS in ~5 min. Note the recovery timeout is `cap(30s)+slack`; with default backoff the post-restart reconnect lands within one backoff ceiling.
If it FAILS (hook stopped attempting) → that is a real regression of the unbounded fix → report to qa-head immediately, do NOT "fix the test".

- [ ] **Step 3: Commit**
```bash
git add tests/hooks/test_ws_forced_fault_soak.py
git commit -m "test(forced-fault): scenario 1 — default-backoff survives >250s outage (beads-63mm)"
```

---

## Task 2: Scenario 2 — literal iptables-drop + recover (`@pytest.mark.soak`)

Proves: a real packet-level connection drop (not a mocked WS-close) is survived and recovered from once the rule is removed. HARD: rule scoped to ephemeral port, reverted, snapshot-diff clean.

**Files:**
- Test: `tests/hooks/test_ws_forced_fault_soak.py::test_iptables_drop_then_recover`

- [ ] **Step 1: Write the iptables helpers (in the same module)**

```python
import subprocess

def _iptables_save() -> str:
    return subprocess.run(["sudo", "iptables-save"],
                          capture_output=True, text=True, check=True).stdout

def _iptables_drop(port: int) -> None:
    assert str(port) != "8377", "refusing to firewall the live daemon port"
    subprocess.run(
        ["sudo", "iptables", "-I", "INPUT", "1", "-p", "tcp",
         "--dport", str(port), "-j", "DROP"],
        check=True,
    )

def _iptables_revert(port: int) -> None:
    # Delete by full spec; loop until the rule is gone (idempotent teardown).
    for _ in range(10):
        r = subprocess.run(
            ["sudo", "iptables", "-D", "INPUT", "-p", "tcp",
             "--dport", str(port), "-j", "DROP"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            break  # no more matching rules
```

- [ ] **Step 2: Write the failing test**

```python
@pytest.mark.soak
async def test_iptables_drop_then_recover(tmp_path, monkeypatch):
    port = _free_port()
    assert str(port) != "8377"
    persistence = tmp_path / "sessions.json"
    agent_path = tmp_path / "ipt-peer"; agent_path.mkdir()
    # ... same env setup as Task 1 (REPOWIRE_DAEMON_*, DISPLAY_NAME, AGENT_PATH, TMUX_PANE) ...
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pane_id: True)
    monkeypatch.setattr(wh, "_get_pane_command", lambda pane_id: "claude")
    monkeypatch.setattr(wh, "_pane_warn_set", lambda pane_id: None)
    monkeypatch.setattr(wh, "_pane_warn_clear", lambda pane_id: None)
    # Small cap so recovery after revert is quick; the DROP itself is literal.
    monkeypatch.setenv("REPOWIRE_WS_RECONNECT_CAP_SEC", "2")

    app = create_test_app(persistence_path=persistence)
    daemon = _LiveDaemon(app, port); daemon.start()
    hook_task = asyncio.create_task(wh.main())
    pre = _iptables_save()
    try:
        peer_id_1 = await _online_peer_id(port)
        _iptables_drop(port)
        try:
            # Daemon's ping_timeout (5s) → it drops the half-open peer; assert
            # the peer goes offline while packets are dropped.
            await _assert_peer_offline(port, timeout=30.0)
        finally:
            _iptables_revert(port)
        # After revert: hook reconnects, same peer_id.
        peer_id_2 = await _online_peer_id(port, timeout=15.0)
        assert peer_id_2 == peer_id_1
    finally:
        hook_task.cancel()
        try:
            await hook_task
        except asyncio.CancelledError:
            pass
        _iptables_revert(port)                 # belt: ensure reverted on any path
        daemon.stop()
        post = _iptables_save()
        assert post == pre, "iptables ruleset leaked — pre/post snapshot differ"
```

Write `_assert_peer_offline(port, timeout)` mirroring `_online_peer_id` but polling for the peer being absent/`status != "online"`. (The daemon marks the half-open peer offline after `ping_timeout`/sweep.) If queries themselves hang because the daemon HTTP port is the same dropped port, query via a separate not-dropped mechanism — NOTE: the DROP is on `--dport <port>` which also blocks the HTTP `/peers` GET. To still observe state, either (a) drop only the WS path is not possible at L3, so instead assert offline by checking that after revert the *reconnect* happened AND log/registry shows a gap, or (b) bind the daemon's HTTP and WS on the same port (they are) and accept that during the drop you cannot query — in that case assert the *observable recovery*: `peer_id_2 == peer_id_1` after revert is sufficient proof the drop interrupted and the hook re-established. **Recommended:** keep it simple — assert recovery (peer online + same peer_id) after revert; drop the mid-drop offline assertion if it requires querying the dropped port. Confirm the chosen approach with qa-head in the TDD red-phase review.

- [ ] **Step 3: Run**

Run: `pytest tests/hooks/test_ws_forced_fault_soak.py::test_iptables_drop_then_recover -m soak -v`
Expected: PASS. Critically verify the final `assert post == pre` passes (no leaked rule). Manually `sudo iptables -S | grep <port>` after → empty.

- [ ] **Step 4: Commit**
```bash
git add tests/hooks/test_ws_forced_fault_soak.py
git commit -m "test(forced-fault): scenario 2 — literal iptables-drop + recover, snapshot-clean (beads-63mm)"
```

---

## Task 3: Scenario 3 — reconnect-storm / no thundering-herd (full jitter)

Proves: when N peers reconnect simultaneously after the daemon returns, `_compute_backoff` full jitter spreads their reconnects over time (no synchronized spike). May compress cap (jitter spread is proportional to cap; the property holds at any cap).

**Files:**
- Test: `tests/hooks/test_ws_forced_fault_soak.py::test_reconnect_storm_is_jittered`

- [ ] **Step 1: Write the failing test**

```python
async def test_reconnect_storm_is_jittered(tmp_path, monkeypatch):
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
    monkeypatch.setenv("REPOWIRE_WS_RECONNECT_CAP_SEC", "4")  # spread over [0,4]s

    N = 8
    # Each hook needs a distinct identity (display name + agent path) so the
    # daemon registers N peers. Spawn N main() tasks with per-task env via a
    # small wrapper that sets REPOWIRE_DISPLAY_NAME/REPOWIRE_AGENT_PATH per task.
    # (Use asyncio.create_task wrapping a coro that os.environ-scopes per peer,
    #  OR start each in its own thread with its own env copy — confirm approach
    #  with qa-head; the simplest reliable form is N threads each running
    #  asyncio.run(main()) with a per-thread monkeypatched env.)

    # 1. Start daemon, bring all N online. Record nothing yet.
    # 2. Kill daemon; all N enter reconnect loop.
    # 3. Restart daemon; instrument the daemon-side connect handler (or poll
    #    /peers and record the wall-clock time each peer_id first becomes online)
    #    to capture per-peer reconnect timestamps.
    # 4. Assert the spread of reconnect timestamps is non-trivial:
    reconnect_times = await _capture_reconnect_times(port, expected=N, ...)
    spread = max(reconnect_times) - min(reconnect_times)
    assert spread > 0.5, f"reconnects not jittered (spread={spread:.3f}s) — herd risk"
    # Optional stronger check: not all within one 50ms bucket.
```

- [ ] **Step 2: Implement `_capture_reconnect_times`**

Poll `GET /peers` rapidly (e.g. every 20ms) after daemon restart; for each peer_id, record the loop-time at which it first appears `online`. Return the list of N first-online times. Stop when all N seen or timeout.

- [ ] **Step 3: Run**

Run: `pytest tests/hooks/test_ws_forced_fault_soak.py::test_reconnect_storm_is_jittered -v`
Expected: PASS; `spread` comfortably > 0.5s with cap=4s and N=8. If flaky near the threshold, raise N or cap, or assert on coefficient-of-variation instead of absolute spread. Avoid asserting an upper bound on spread (jitter is random).

- [ ] **Step 4: Commit**
```bash
git add tests/hooks/test_ws_forced_fault_soak.py
git commit -m "test(forced-fault): scenario 3 — reconnect storm is jittered, no herd (beads-63mm)"
```

---

## Task 4: Scenario 4 — Fix C pane indicator appears after grace + clears on recover

Proves: against a REAL throwaway tmux pane, the WS-lost indicator (`pane title "⚠ repowire WS lost"`) appears only after the grace threshold (`_WARN_AFTER_ATTEMPTS = 3`) and is cleared on reconnect; no stdin injection (active turn unharmed). `_pane_warn_set`/`_pane_warn_clear` are NOT monkeypatched here — we want the real tmux calls.

**Files:**
- Test: `tests/hooks/test_ws_forced_fault_soak.py::test_fix_c_pane_indicator_lifecycle`

- [ ] **Step 1: Throwaway tmux pane helper**

```python
import contextlib, os

@contextlib.contextmanager
def _tmux_throwaway_pane():
    session = f"ff-soak-{os.getpid()}"
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "80", "-y", "24",
                    "sleep", "100000"], check=True)
    try:
        pane_id = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
            capture_output=True, text=True, check=True).stdout.strip()
        yield session, pane_id
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)

def _pane_title(pane_id: str) -> str:
    return subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_title}"],
        capture_output=True, text=True, check=True).stdout.strip()

def _pane_capture(pane_id: str) -> str:
    return subprocess.run(["tmux", "capture-pane", "-p", "-t", pane_id],
                          capture_output=True, text=True, check=True).stdout
```

- [ ] **Step 2: Write the failing test**

```python
async def test_fix_c_pane_indicator_lifecycle(tmp_path, monkeypatch):
    port = _free_port()
    assert str(port) != "8377"
    persistence = tmp_path / "sessions.json"
    agent_path = tmp_path / "fixc-peer"; agent_path.mkdir()
    with _tmux_throwaway_pane() as (session, pane_id):
        monkeypatch.setenv("REPOWIRE_DAEMON_HOST", "127.0.0.1")
        monkeypatch.setenv("REPOWIRE_DAEMON_PORT", str(port))
        monkeypatch.setenv("REPOWIRE_CIRCLE", "default")
        monkeypatch.setenv("REPOWIRE_DISPLAY_NAME", "fixc-peer")
        monkeypatch.setenv("REPOWIRE_AGENT_PATH", str(agent_path))
        monkeypatch.setenv("TMUX_PANE", pane_id)
        monkeypatch.setenv("REPOWIRE_WS_RECONNECT_CAP_SEC", "1")  # reach attempt>=3 fast
        # Real pane → keep _is_pane_safe truthful but cheap: the pane runs
        # `sleep`, so let _get_pane_command return the real command. The
        # snapshot _expected_command is taken in main() from the real pane.
        # Do NOT patch _pane_warn_set/_clear — we assert the REAL tmux effect.
        # _is_pane_safe must stay True throughout; patch it to True to avoid
        # depending on pane_current_command matching across the test.
        monkeypatch.setattr(wh, "_is_pane_safe", lambda pid: True)
        monkeypatch.setattr(wh, "_get_pane_command", lambda pid: "sleep")

        before = _pane_capture(pane_id)
        assert _pane_title(pane_id) != "⚠ repowire WS lost"

        app = create_test_app(persistence_path=persistence)
        daemon = _LiveDaemon(app, port); daemon.start()
        hook_task = asyncio.create_task(wh.main())
        try:
            await _online_peer_id(port)
            assert _pane_title(pane_id) != "⚠ repowire WS lost"  # no warn while healthy

            daemon.stop()  # trigger reconnect loop → after attempt>=3, warn
            # Poll until the title flips (bounded — cap=1s, need ≥3 attempts).
            await _wait_until(lambda: _pane_title(pane_id) == "⚠ repowire WS lost",
                              timeout=20.0)

            # Recover → indicator cleared.
            daemon2 = _LiveDaemon(create_test_app(persistence_path=persistence), port)
            daemon2.start()
            try:
                await _online_peer_id(port, timeout=15.0)
                await _wait_until(lambda: _pane_title(pane_id) == "", timeout=15.0)
            finally:
                daemon2.stop()

            # No stdin injection: the pane buffer must not have gained injected
            # keystrokes/commands from the warning (title/display-message only).
            after = _pane_capture(pane_id)
            assert "repowire" not in after.replace(before, ""), \
                "Fix C must not inject into the pane buffer (stdin-safe)"
        finally:
            hook_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hook_task
```

Implement `_wait_until(predicate, timeout)` as an async poll helper (sleep 50ms between checks; `predicate` is sync tmux query run via `asyncio.to_thread` if needed).

- [ ] **Step 3: Run**

Run: `pytest tests/hooks/test_ws_forced_fault_soak.py::test_fix_c_pane_indicator_lifecycle -v`
Expected: PASS. Title flips to the warning after the daemon dies (≥3 attempts at cap=1s ≈ within seconds) and clears on recover. The buffer-diff assertion proves no stdin injection. NOTE on the buffer-diff: `display-message` renders in the status line, not the pane buffer, so `capture-pane` should not contain the transient text — tune the assertion if your tmux renders status differently; the load-bearing checks are the title set/clear + absence of injected keystrokes.

- [ ] **Step 4: Commit**
```bash
git add tests/hooks/test_ws_forced_fault_soak.py
git commit -m "test(forced-fault): scenario 4 — Fix C pane indicator lifecycle, stdin-safe (beads-63mm)"
```

---

## Task 5: Scenario 5 — intentional-marker guard (peek-only, no resurrection)

Proves: when a fresh `.shutdown-intentional` / `.restart-intentional` marker exists, `supervise()` does NOT re-enter `main()` (no peer resurrection); when absent, it DOES re-enter. The marker is peeked (stat), never unlinked. **HARD: marker dir is monkeypatched to `tmp_path` + a fake role — never the live `~/ai-infra/ops/`.**

**Files:**
- Test: `tests/hooks/test_ws_forced_fault_soak.py::test_marker_guard_*`

- [ ] **Step 1: Write the failing tests (two cases)**

```python
def test_marker_guard_blocks_resurrection(tmp_path, monkeypatch):
    # ISOLATION: redirect marker dir to tmp + fake role. NEVER live ~/ai-infra/ops.
    fake_role = "forced-fault-fake-role"
    marker_base = tmp_path / "ops" / fake_role
    marker_base.mkdir(parents=True)
    monkeypatch.setattr(wh, "_marker_dir", lambda role: tmp_path / "ops" / role)
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: fake_role)
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_is_pane_safe", lambda pid: True)
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)

    # Fresh intentional marker (mtime = now → age < 300s).
    marker = marker_base / ".shutdown-intentional"
    marker.write_text("")

    calls = {"n": 0}
    def _fake_main():
        calls["n"] += 1
        return 1  # crash-like return → would normally trigger respawn
    monkeypatch.setattr(wh, "main", _fake_main)
    # supervise() calls asyncio.run(main()); patch main to a sync fn won't work
    # with asyncio.run. Instead patch main to an async fn:
    async def _fake_main_async():
        calls["n"] += 1
        return 1
    monkeypatch.setattr(wh, "main", _fake_main_async)

    rc = wh.supervise()
    assert calls["n"] == 1, "supervise re-entered main despite fresh marker (resurrection!)"
    assert rc == 1
    assert marker.exists(), "marker was consumed — must be peek-only (stat, no unlink)"


def test_marker_guard_absent_allows_respawn(tmp_path, monkeypatch):
    fake_role = "forced-fault-fake-role"
    (tmp_path / "ops" / fake_role).mkdir(parents=True)
    monkeypatch.setattr(wh, "_marker_dir", lambda role: tmp_path / "ops" / role)
    monkeypatch.setattr(wh, "_resolve_agent_role", lambda: fake_role)
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(wh, "_compute_backoff", lambda *a, **k: 0.0)
    # No marker file → supervise should re-enter until pane goes unsafe.
    safe_calls = {"n": 0}
    def _pane_safe(pid):
        # First crash: pane still safe → respawn. After 2nd main(), pane unsafe → stop.
        return safe_calls["n"] < 2
    monkeypatch.setattr(wh, "_is_pane_safe", _pane_safe)
    calls = {"n": 0}
    async def _fake_main_async():
        calls["n"] += 1
        safe_calls["n"] += 1
        return 1
    monkeypatch.setattr(wh, "main", _fake_main_async)

    rc = wh.supervise()
    assert calls["n"] >= 2, "supervise did not re-enter main when no marker present"
```

Note the staleness branch is worth a third case if cheap: write the marker with an old mtime (`os.utime(marker, (t, t))` where `t = time.time() - 400`) and assert `supervise` re-enters (stale marker = crash-after-write, not honored). Confirm with qa-head whether to include.

- [ ] **Step 2: Run**

Run: `pytest tests/hooks/test_ws_forced_fault_soak.py -k marker_guard -v`
Expected: both PASS. If `test_marker_guard_blocks_resurrection` fails on `marker.exists()` → the hook is consuming the marker (NOT peek-only) → real regression, report to qa-head.

- [ ] **Step 3: Commit**
```bash
git add tests/hooks/test_ws_forced_fault_soak.py
git commit -m "test(forced-fault): scenario 5 — marker guard peek-only, isolated marker dir (beads-63mm)"
```

---

## Task 6: Full suite run + PR

- [ ] **Step 1: Run the fast subset (CI-equivalent)**

Run: `pytest tests/hooks/test_ws_forced_fault_soak.py -m "not soak" -v`
Expected: scenarios 3, 4, 5 PASS (4 may be slow but is not a 5-min soak).

- [ ] **Step 2: Run the full soak set**

Run: `pytest tests/hooks/test_ws_forced_fault_soak.py -m soak -v` (then a full `pytest tests/hooks/test_ws_forced_fault_soak.py -v`)
Expected: ALL 5 scenarios PASS. Record timings. Manually verify after: `sudo iptables -S | grep <ports>` empty; `tmux ls` shows no `ff-soak-*` session; `ls ~/ai-infra/ops/*/` untouched (no stray markers).

- [ ] **Step 3: Confirm no leak / no live-mesh touch (verification-before-completion)**

- `sudo iptables-save` matches the pre-run baseline.
- No `ff-soak-*` tmux session lingering.
- `~/ai-infra/ops/<real-role>/` contains no test-created `.shutdown-intentional` / `.restart-intentional`.
- Live `repowire.service` on `:8377` and live peers (`list_peers`) unaffected throughout.

- [ ] **Step 4: Open PR**
```bash
git push -u origin test/evl-forced-fault-soak
gh pr create --title "test(forced-fault): beads-63mm literal soak verification of beads-evl fix" \
  --body "Independent forced-fault verification of PR #26 (beads-evl, merge c841069) across 5 literal scenarios. All isolated on ephemeral daemons; live :8377 and agent-gateway marker dirs untouched. See docs/superpowers/plans/2026-06-16-63mm-forced-fault-soak.md."
```
Report PR # + per-scenario empirical results to qa-head. Do NOT merge — qa-head reviews; deploy is devops-head on director's signal.

---

## Self-Review (qa-head, against beads-63mm DoD + design spec §7)

- **Scenario 1 (daemon-down >250s default backoff, beat old 50-cap):** Task 1 ✓ — DEFAULT backoff (no `_compute_backoff` patch), 260s outage, asserts still-attempting past 250s + peer_id reuse on recover.
- **Scenario 2 (literal iptables-drop + recover):** Task 2 ✓ — real `iptables -I INPUT 1 --dport <port> DROP`, scoped, reverted, pre/post snapshot equality, `:8377` guard.
- **Scenario 3 (reconnect-storm / jitter):** Task 3 ✓ — N peers, kill+restart, asserts reconnect-time spread (full jitter).
- **Scenario 4 (Fix C grace + clear, turn unharmed):** Task 4 ✓ — real tmux pane, title appears after attempt≥3, clears on recover, buffer-diff asserts stdin-safe.
- **Scenario 5 (marker guard, peek-only):** Task 5 ✓ — isolated tmp marker dir + fake role, asserts no resurrection + marker survives (no unlink) + respawn when absent.
- **Mesh-safety:** `:8377` guard, iptables snapshot-diff, marker-dir monkeypatch to tmp, throwaway tmux session — all HARD invariants encoded up front.
- **CI vs independent:** scenarios 1 & 2 `@pytest.mark.soak` (excluded from CI); 3–5 run normally.
