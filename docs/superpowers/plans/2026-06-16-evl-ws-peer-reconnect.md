# Peer-side WS reconnect resilience + pane-warning — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать так, чтобы peer-side ws-hook не умирал навсегда при длинном WS-обрыве (rate-limit cascade / daemon-down), переподключался бесконечно с capped backoff+jitter, корректно различал намеренный shutdown, и сигналил пользователю в pane без stdin-инъекции.

**Architecture:** Рефакторим `repowire/hooks/websocket_hook.py`: (1) `main()` — unbounded reconnect-loop с pane-safety guard и capped exp backoff + full jitter; (2) `supervise()` — внешний loop в том же файле, перезапускает `main()` при краше, гейт = pane-safe AND нет свежего intentional-маркера (peek-only); (3) tmux-warning helpers для видимого индикатора потери WS. Daemon-side (sweep/disconnect) НЕ трогаем — уже в проде (q2ok/nxxm).

**Tech Stack:** Python 3.12, asyncio, `websockets`, pytest + `pytest-asyncio`, `unittest.mock.patch`/`monkeypatch`, tmux CLI.

**Spec:** `docs/superpowers/specs/2026-06-16-evl-ws-peer-reconnect-design.md`
**Issue:** beads-evl. **Worktree/branch:** `fix/ws-peer-reconnect-deadzone`.

**Cross-repo guard (ВАЖНО):** этот PR — ТОЛЬКО `repowire-fork`. Если Task 0 покажет, что роль агента не доступна в env hook-процесса и нужна правка `spawn-claude`/`system/` — НЕ добавлять в этот PR. Остановиться, сообщить devops-head: заведём отдельный subtask `metadata.repo=system`. Watchdog-marker-guard (Task 4) тогда временно деградирует до pane-safety-only, остальное продолжается.

---

### Task 0: Investigate marker-path env availability (decision gate, no code)

Маркеры лежат в `$HOME/ai-infra/ops/<role>/.shutdown-intentional` и `.restart-intentional` (one-shot, max-age 300s — см. `system/ops/bin/agent-gateway.py:_check_marker`). `<role>` — это agent role-dir (`director`, `devops-head`, …), НЕ `REPOWIRE_PEER_ROLE` (тот = `agent`/`head`/…).

- [ ] **Step 1: Определить, видит ли hook-процесс свою роль**

Run:
```bash
# В живой сессии любого агента посмотреть env ws-hook процесса:
pid=$(cat ~/ai-infra/ops/repowire/ws-hook-*.pid 2>/dev/null | head -1)
[ -n "$pid" ] && tr '\0' '\n' < /proc/$pid/environ | grep -iE 'ROLE|AGENT|SCOPE_NAME|BRAIN_AGENT' || echo "no ws-hook pid found"
# И что экспортирует spawn-claude:
grep -nE 'export (BRAIN_AGENT_ROLE|REPOWIRE_PEER_ROLE|SCOPE_NAME|AGENT_ROLE)' ~/repos/agents-brain-team/system/ops/bin/spawn-claude.sh
```
Expected: найти env-переменную, из которой выводится `<role>` для marker-dir (вероятные кандидаты: `SCOPE_NAME`, `BRAIN_AGENT_ROLE`).

- [ ] **Step 2: Решение**

- Если подходящая env есть → зафиксировать её имя, использовать в Task 4 (`_resolve_agent_role()`). Продолжать план целиком.
- Если НЕТ надёжной env → **СТОП по Task 4 marker-guard**. Сообщить devops-head: нужна cross-repo правка `system/` (экспорт роли в env hook). Завести отдельный subtask `metadata.repo=system`. В ЭТОМ PR Task 4 реализовать с pane-safety-only (без marker-peek), пометив `# TODO(beads-evl cross-repo): add marker guard once role env shipped` НЕ оставлять — вместо TODO сделать `_resolve_agent_role()` возвращающим `None` при отсутствии env, а `_marker_present(None) -> False` (безопасная деградация). Tasks 1–3, 5, 6 не затронуты.

- [ ] **Step 3: Commit (документ решения)**

Зафиксировать вывод в описании beads-evl коммента нет — записать в PR-описание раздел «Task 0 outcome». Кода нет, коммита нет.

---

### Task 1: Capped exponential backoff + full jitter helper

**Files:**
- Modify: `repowire/hooks/websocket_hook.py` (добавить helper + import `random`)
- Test: `tests/hooks/test_websocket_hook_reconnect.py` (создать)

- [ ] **Step 1: Написать падающий тест**

```python
# tests/hooks/test_websocket_hook_reconnect.py
from __future__ import annotations
import pytest
import repowire.hooks.websocket_hook as wh


def test_backoff_capped_and_jittered(monkeypatch):
    # full jitter: delay ∈ [0, min(cap, base*2**attempt)]
    monkeypatch.setattr(wh.random, "uniform", lambda a, b: b)  # take upper bound
    assert wh._compute_backoff(attempt=0, cap=30.0, base=1.0) == 1.0
    assert wh._compute_backoff(attempt=3, cap=30.0, base=1.0) == 8.0
    assert wh._compute_backoff(attempt=10, cap=30.0, base=1.0) == 30.0  # capped


def test_backoff_lower_bound_is_zero(monkeypatch):
    monkeypatch.setattr(wh.random, "uniform", lambda a, b: a)  # take lower bound
    assert wh._compute_backoff(attempt=5, cap=30.0, base=1.0) == 0.0
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_compute_backoff'`.

- [ ] **Step 3: Минимальная реализация**

В `websocket_hook.py` добавить `import random` (рядом с другими import'ами) и helper:
```python
# Reconnect backoff cap — env-overridable so regression tests can compress the
# >250s daemon-down window that used to exhaust the old 50-attempt cap.
_RECONNECT_CAP_SEC = float(os.environ.get("REPOWIRE_WS_RECONNECT_CAP_SEC", "30"))


def _compute_backoff(attempt: int, cap: float = _RECONNECT_CAP_SEC, base: float = 1.0) -> float:
    """Full-jitter capped exponential backoff.

    Returns a delay in [0, min(cap, base * 2**attempt)]. Full jitter spreads
    simultaneous peer reconnects after a long daemon outage (anti
    thundering-herd / reconnect-storm — same class as q2ok singleton-conflict).
    """
    ceiling = min(cap, base * (2 ** attempt))
    return random.uniform(0.0, ceiling)
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -v`
Expected: PASS (оба теста).

- [ ] **Step 5: Commit**

```bash
git add repowire/hooks/websocket_hook.py tests/hooks/test_websocket_hook_reconnect.py
git commit -m "feat(ws-hook): capped exp backoff + full jitter helper (beads-evl)"
```

---

### Task 2: Unbounded reconnect loop + pane-safety guard в `main()`

**Files:**
- Modify: `repowire/hooks/websocket_hook.py:main()` (текущий цикл `while attempt < max_attempts`, ~L538-619)
- Test: `tests/hooks/test_websocket_hook_reconnect.py`

- [ ] **Step 1: Написать падающий тест**

Тест проверяет, что (а) цикл НЕ выходит после >50 неуспешных connect, (б) выходит, когда pane становится unsafe. Тестируем через подмену `websockets.connect` (бросает) и `_is_pane_safe`.

```python
@pytest.mark.asyncio
async def test_main_exits_when_pane_unsafe(monkeypatch):
    monkeypatch.setenv if False else None  # noqa (placeholder removed below)
```

Заменить плейсхолдер на реальный тест:
```python
import asyncio
from unittest.mock import patch


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
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -k main -v`
Expected: FAIL — текущий `main()` выходит после 50 (`test_main_retries_past_old_50_cap` не дойдёт до 60) и не имеет верхнего pane-safety guard на входе.

- [ ] **Step 3: Минимальная реализация**

Переписать цикл в `main()`. Убрать `max_attempts`/`attempt < max_attempts`. Структура:
```python
    attempt = 0
    while True:
        if not _is_pane_safe(pane_id):
            logger.info("Pane %s no longer safe, stopping reconnect loop", pane_id)
            clear_pane_runtime_state(pane_id)
            return 0
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=5) as websocket:
                attempt = 0
                _pane_warn_clear(pane_id)              # Task 5
                # ... existing connect_msg send + connected handling ...
                # ... existing message loop ...
        except websockets.exceptions.ConnectionClosed as e:
            attempt += 1
            logger.warning("Connection closed (attempt %d): code=%s", attempt, e.code)
        except (websockets.exceptions.WebSocketException, OSError) as e:
            attempt += 1
            logger.warning("Connection error (attempt %d): %s", attempt, e)
        if attempt >= _WARN_AFTER_ATTEMPTS:             # Task 5
            _pane_warn_set(pane_id)
        await asyncio.sleep(_compute_backoff(attempt))
```
Сохранить существующую внутреннюю логику connect_msg / `connected` / `PaneUnsafeError` (на `PaneUnsafeError` из message-loop по-прежнему `clear_pane_runtime_state` + `return 0`). Удалить финальный `logger.error("Exhausted ...")` + `return 1`.

ПРИМЕЧАНИЕ: `_pane_warn_set/_clear` определяются в Task 5 — при выполнении строго по порядку добавить их как no-op заглушки сейчас НЕ нужно; реализуйте Task 5 ПЕРЕД этим шагом ИЛИ временно закомментируйте две warn-строки и раскомментируйте в Task 5. Рекомендуется: сделать Task 5 перед Task 2 если исполняете вручную; subagent-driven — порядок задан, добавьте минимальные заглушки `def _pane_warn_set(p): pass` / `_clear` и замените в Task 5.

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -k main -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add repowire/hooks/websocket_hook.py tests/hooks/test_websocket_hook_reconnect.py
git commit -m "feat(ws-hook): unbounded reconnect with pane-safety guard (beads-evl)"
```

---

### Task 3: `_marker_present` peek-only helper

**Files:**
- Modify: `repowire/hooks/websocket_hook.py`
- Test: `tests/hooks/test_websocket_hook_reconnect.py`

- [ ] **Step 1: Написать падающий тест**

```python
def test_marker_present_peek_does_not_unlink(tmp_path, monkeypatch):
    role_dir = tmp_path / "ops" / "devops-head"
    role_dir.mkdir(parents=True)
    marker = role_dir / ".shutdown-intentional"
    marker.write_text("")
    monkeypatch.setattr(wh, "_marker_dir", lambda role: role_dir)
    assert wh._marker_present("devops-head") is True
    assert marker.exists()  # peek-only — NOT consumed (gateway owns consumption)


def test_marker_present_stale_is_false(tmp_path, monkeypatch):
    import os, time
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
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -k marker -v`
Expected: FAIL — нет `_marker_present`/`_marker_dir`.

- [ ] **Step 3: Минимальная реализация**

```python
_INTENTIONAL_MARKER_MAX_AGE_SEC = 300  # mirror agent-gateway _check_marker


def _marker_dir(role: str):
    from pathlib import Path
    return Path(os.path.expanduser("~")) / "ai-infra" / "ops" / role


def _marker_present(role: str | None) -> bool:
    """Peek (no unlink) for a fresh intentional shutdown/restart marker.

    PEEK-ONLY: the marker is one-shot consumed by agent-gateway.monitor_loop;
    the hook must NOT unlink it. Returns True iff a fresh (<300s)
    .shutdown-intentional or .restart-intentional exists for `role`.
    role=None (no role env, see Task 0) → False (degrade to pane-safety only).
    """
    if not role:
        return False
    base = _marker_dir(role)
    for name in (".shutdown-intentional", ".restart-intentional"):
        marker = base / name
        try:
            age = time.time() - marker.stat().st_mtime
        except (FileNotFoundError, OSError):
            continue
        if age <= _INTENTIONAL_MARKER_MAX_AGE_SEC:
            return True
    return False
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -k marker -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add repowire/hooks/websocket_hook.py tests/hooks/test_websocket_hook_reconnect.py
git commit -m "feat(ws-hook): peek-only intentional-marker helper (beads-evl)"
```

---

### Task 4: `supervise()` watchdog outer loop

**Files:**
- Modify: `repowire/hooks/websocket_hook.py` (+ `__main__`)
- Test: `tests/hooks/test_websocket_hook_reconnect.py`

- [ ] **Step 1: Написать падающий тест**

```python
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
    rc = wh.supervise()
    assert calls["n"] == 1  # pane unsafe → no respawn
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -k supervise -v`
Expected: FAIL — нет `supervise`/`_resolve_agent_role`.

- [ ] **Step 3: Минимальная реализация**

`_resolve_agent_role()` — использовать env, найденную в Task 0 (пример с `SCOPE_NAME`; адаптировать по факту Task 0; вернуть `None` если не выводится):
```python
def _resolve_agent_role() -> str | None:
    """Agent role-dir name for the marker path ($HOME/ai-infra/ops/<role>/).

    Resolved from the spawn env discovered in Task 0. Returns None when no
    reliable role env exists → _marker_present degrades to False (pane-safety
    guard still applies). NOTE: REPOWIRE_PEER_ROLE is the mesh role
    (agent/head), NOT the role-dir — do not use it here.
    """
    role = os.environ.get("BRAIN_AGENT_ROLE") or os.environ.get("SCOPE_NAME")
    return role or None


def supervise() -> int:
    """Outer watchdog: re-enter main() on crash while the pane is alive and no
    intentional shutdown/restart is in progress. Defense-in-depth for the rare
    case where main() dies on an unhandled exception (unbounded reconnect
    already covers normal WS drops).
    """
    role = _resolve_agent_role()
    pane_id = os.environ.get("TMUX_PANE")
    while True:
        try:
            rc = asyncio.run(main())
        except KeyboardInterrupt:
            return 0
        except Exception:
            logger.exception("ws-hook main() crashed; evaluating respawn")
            rc = 1
        if rc == 0:
            return 0  # clean pane-unsafe exit from main()
        if pane_id and not _is_pane_safe(pane_id):
            logger.info("pane unsafe after crash; not respawning")
            return rc
        if _marker_present(role):
            logger.info("intentional marker present after crash; not respawning")
            return rc
        time.sleep(_compute_backoff(1))
```
Заменить `__main__`:
```python
if __name__ == "__main__":
    try:
        sys.exit(supervise())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -k supervise -v`
Expected: PASS (все три).

- [ ] **Step 5: Commit**

```bash
git add repowire/hooks/websocket_hook.py tests/hooks/test_websocket_hook_reconnect.py
git commit -m "feat(ws-hook): supervise() watchdog with pane+marker guard (beads-evl)"
```

---

### Task 5: Fix C — tmux pane-warning (no stdin injection)

**Files:**
- Modify: `repowire/hooks/websocket_hook.py`
- Test: `tests/hooks/test_websocket_hook_reconnect.py`

- [ ] **Step 1: Написать падающий тест**

```python
def test_pane_warn_set_uses_display_message_not_send_keys(monkeypatch):
    cmds = []
    monkeypatch.setattr(wh.subprocess, "run", lambda args, **k: cmds.append(args))
    wh._pane_warn_set("%1")
    flat = [" ".join(c) for c in cmds]
    assert any("display-message" in f for f in flat)
    assert all("send-keys" not in f for f in flat)   # NEVER stdin injection
    assert all("display-popup" not in f for f in flat)


def test_pane_warn_clear_resets_indicator(monkeypatch):
    cmds = []
    monkeypatch.setattr(wh.subprocess, "run", lambda args, **k: cmds.append(args))
    wh._pane_warn_set("%1")
    cmds.clear()
    wh._pane_warn_clear("%1")
    flat = [" ".join(c) for c in cmds]
    assert any("select-pane" in f or "set-option" in f for f in flat)


def test_warn_threshold_constant_present():
    assert isinstance(wh._WARN_AFTER_ATTEMPTS, int) and wh._WARN_AFTER_ATTEMPTS >= 1
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -k warn -v`
Expected: FAIL — нет `_pane_warn_set`/`_pane_warn_clear`/`_WARN_AFTER_ATTEMPTS`.

- [ ] **Step 3: Минимальная реализация**

```python
# Surface the WS-lost warning only after the disconnect persists, so a
# momentary blip does not flap the indicator.
_WARN_AFTER_ATTEMPTS = 3
_warn_active = False


def _pane_warn_set(pane_id: str) -> None:
    """Show a visible WS-lost warning in the pane WITHOUT touching stdin.

    Persistent indicator via pane title + a one-shot transient status message.
    Best-effort: tmux errors are swallowed and never break the reconnect loop.
    NEVER use send-keys/paste-buffer/display-popup (would corrupt Claude's turn).
    """
    global _warn_active
    try:
        subprocess.run(
            ["tmux", "select-pane", "-t", pane_id, "-T", "⚠ repowire WS lost"],
            capture_output=True,
        )
        if not _warn_active:
            subprocess.run(
                ["tmux", "display-message", "-t", pane_id,
                 "repowire: WS соединение потеряно, переподключаюсь…"],
                capture_output=True,
            )
    except Exception as e:  # pragma: no cover
        logger.debug("pane_warn_set failed: %s", e)
    _warn_active = True


def _pane_warn_clear(pane_id: str) -> None:
    """Clear the WS-lost indicator on successful reconnect. Best-effort."""
    global _warn_active
    if not _warn_active:
        return
    try:
        subprocess.run(
            ["tmux", "select-pane", "-t", pane_id, "-T", ""],
            capture_output=True,
        )
    except Exception as e:  # pragma: no cover
        logger.debug("pane_warn_clear failed: %s", e)
    _warn_active = False
```
Если в Task 2 были временные заглушки `_pane_warn_set/_clear` — удалить их, оставив эти реализации; убедиться, что вызовы в `main()` (`_pane_warn_clear` на connect, `_pane_warn_set` при `attempt >= _WARN_AFTER_ATTEMPTS`) активны.

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/hooks/test_websocket_hook_reconnect.py -k warn -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add repowire/hooks/websocket_hook.py tests/hooks/test_websocket_hook_reconnect.py
git commit -m "feat(ws-hook): visible pane-warning on sustained WS loss (beads-evl)"
```

---

### Task 6: Real forced-fault regression (socket-kill, env-compressed)

**Files:**
- Test: `tests/hooks/test_ws_reconnect_forced_fault.py` (создать)

Доказывает: реальный (не мок) обрыв соединения с daemon на окно, заведомо превышающее старый 50-cap (через малый `REPOWIRE_WS_RECONNECT_CAP_SEC`), → hook переподключается и восстанавливает регистрацию (peer_id reuse). Литеральный >250s iptables-soak — НЕ здесь (beads-63mm, отдельный qa-прогон).

- [ ] **Step 1: Написать тест по существующим daemon-fixture паттернам**

Использовать паттерн из `tests/daemon/test_peer_registry_reconnect.py` и `tests/daemon/test_transport_disconnect_closes_socket.py` (как они поднимают транспорт/daemon). Скелет:
```python
# tests/hooks/test_ws_reconnect_forced_fault.py
"""beads-evl: real socket-kill forced-fault — hook survives a daemon outage
longer than the old 50-attempt cap and re-registers on recovery.

Backoff cap is compressed via REPOWIRE_WS_RECONNECT_CAP_SEC so the test proves
'unbounded, beats 50-cap' without a literal >250s wall-clock wait.
"""
from __future__ import annotations
import asyncio
import pytest

pytestmark = pytest.mark.asyncio


async def test_hook_reconnects_after_real_socket_kill(monkeypatch, ...):
    # 1. start a real daemon (reuse the daemon test fixture).
    # 2. connect the ws-hook (or a thin client driving websocket_hook.main()).
    # 3. assert it registered (transport.is_connected / peer_registry online).
    # 4. KILL the daemon process/socket (real close, not a mock WS-drop).
    # 5. keep it down long enough that, at the compressed backoff, the hook
    #    makes >50 reconnect attempts (assert attempt counter or log).
    # 6. restart the daemon on the same port.
    # 7. assert: hook reconnected AND re-registered with the SAME peer_id
    #    (identity reuse via (path, circle, backend)).
    ...
```
ИМПЛЕМЕНТАЦИЯ: worker дописывает по фактическим fixture'ам репозитория. Обязательные ассерты: (а) реальный kill сокета, (б) >50 attempt'ов пережиты, (в) re-registration с тем же peer_id после recovery.

- [ ] **Step 2: Запустить — убедиться, что падает (до фикса прошёл бы только при unbounded)**

Run: `pytest tests/hooks/test_ws_reconnect_forced_fault.py -v`
Expected: PASS на новом коде; для контроля — временно вернуть `max_attempts=50` локально и убедиться, что тест ловит регрессию (FAIL), затем откатить.

- [ ] **Step 3: Commit**

```bash
git add tests/hooks/test_ws_reconnect_forced_fault.py
git commit -m "test(ws-hook): real socket-kill reconnect regression (beads-evl)"
```

---

### Task 7: Full verification + lint

- [ ] **Step 1: Прогнать весь hook-сьют + ранее зелёные daemon-тесты**

Run: `pytest tests/hooks/ tests/daemon/ -v`
Expected: PASS, без регрессий в существующих `test_websocket_hook_*`, `test_ws_liveness_tick`, `test_transport_disconnect_closes_socket`.

- [ ] **Step 2: Линт/типы по конвенции репозитория**

Run: `ruff check repowire/hooks/websocket_hook.py tests/hooks/ && mypy repowire/hooks/websocket_hook.py` (или то, что использует репозиторий — сверить с CI/pyproject).
Expected: чисто.

- [ ] **Step 3: Открыть PR (English title/body), сообщить devops-head**

PR в `repowire-fork`, ветка `fix/ws-peer-reconnect-deadzone`. В теле: ссылка на spec/plan, раздел «Task 0 outcome», явно отметить «daemon-side untouched», «no cross-repo changes» (или: «cross-repo system/ change escalated separately»). НЕ мержить сам — review за devops-head.

---

## Self-Review (выполнено автором плана)

- **Spec coverage:** Fix A.1 → Task 1+2; Fix A.2 → Task 3+4; Fix C → Task 5; real forced fault → Task 6; marker-path env-риск (§10.1) → Task 0 + graceful `None`-degradation; >250s soak (§7) → вне плана (beads-63mm). Покрыто.
- **Placeholder scan:** убран плейсхолдерный псевдо-шаг в Task 2 Step 1; Task 6 намеренно оставляет fixture-детали worker'у (реальные daemon-fixture репозитория), но фиксирует обязательные ассерты — это не «implement later», а делегирование к конкретным существующим паттернам.
- **Type consistency:** `_compute_backoff(attempt, cap, base)`, `_marker_present(role|None)`, `_marker_dir(role)`, `_resolve_agent_role()->str|None`, `_pane_warn_set/_clear(pane_id)`, `_WARN_AFTER_ATTEMPTS:int`, `supervise()->int` — имена согласованы между задачами и тестами.
- **Cross-repo:** Task 0 гейт + header-предупреждение держат PR в одной репе.
