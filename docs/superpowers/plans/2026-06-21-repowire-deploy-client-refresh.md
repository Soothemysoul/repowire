# repowire deploy-time client-refresh + orphan-reaper — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать repowire-fork один атомарный deploy-скрипт, который выкатывает новый код пакета и без потери работы рефрешит живые сессии, плюс reaper, вычищающий orphan websocket_hook/mcp процессы, не привязанные к живой tmux-панели.

**Architecture:** Два независимых артефакта в `repowire-fork/scripts/`. (1) **Reaper** — pure-функция диффа (`find_orphans`) поверх трёх входов: список repowire-процессов из `ps`, live-pane set из `tmux list-panes`, live-peer set из daemon `GET /peers`; тонкий `main()` собирает входы и kill'ит только непривязанные процессы (dry-run по умолчанию). Полностью независим от backend-части rz1g — строится и тестируется сразу. (2) **Deploy** — bash-оркестратор, выполняющий АТОМАРНО и fail-fast: `uv tool reinstall` (из чекаута/origin) → `systemctl --user restart repowire` → health-wait → `POST /control/refresh-clients` (контракт из rz1g) → reaper. Refresh-POST вынесен в маленький Python-хелпер (httpx) для чистой работы с auth-токеном.

**Tech Stack:** Python 3.12 (click CLI, httpx, pytest), bash (`set -euo pipefail`, shellcheck), systemd --user, uv tool, tmux.

---

## Frozen contract (backend-head, beads-rz1g — SSOT in beads-ii5m)

Реализуем СТРОГО против него; менять только через backend-head (circle `project-agents-brain-team`):

- **Endpoint:** `POST /control/refresh-clients` на daemon HTTP-листенере (тот же `cfg.daemon.host:port`, по умолчанию `http://127.0.0.1:8377` — тот же, что отдаёт `GET /peers`).
- **Body:** `{ "target_epoch": <str|optional>, "reason": <str>, "scope": <"workers"|"all"|"advisory"> }` → `200`.
- **WS msg** (рассылает daemon, НЕ наша забота): `{"type": "refresh", target_epoch, reason, scope}`.
- **Handshake** отдаёт текущий `refresh_epoch` (закрывает гонку реконнекта; тоже backend).
- **Auth:** `Depends(require_auth)` (HTTPBearer) + `require_localhost` (control-оп, deploy всегда локальный). Enforced ТОЛЬКО если `cfg.daemon.auth_token` задан. **Клиент берёт токен из env `$REPOWIRE_AUTH_TOKEN`** (НЕ парсит конфиг). Шлёт `Authorization: Bearer $REPOWIRE_AUTH_TOKEN` если env задан; если auth_token в конфиге пуст — заголовок игнорируется, `200`. `daemon_url` берём из env-override с дефолтом `127.0.0.1:8377` (не хардкод — зеркалит `cfg.daemon`).

### CONFIRMED by backend-head (notif-d800fdec, 2026-06-21)

**Вывод `target_epoch`.** Подтверждено: `target_epoch` **опционален** — deploy его НЕ вычисляет, НЕ передаёт и НЕ дёргает GET. Daemon после restart уже загрузил новый код и подставляет свой текущий deployed-epoch сам (той же формулой version+sha/mtime, что сессии для loaded-epoch). Это исключает рассинхрон deploy↔daemon. POST шлём только `{reason, scope}`. backend-head проводит как refinement-владелец контракта, уведомляет director (SSOT в ii5m).

**Канонический curl (от backend-head):**
```bash
curl -sS -X POST "http://127.0.0.1:8377/control/refresh-clients" \
  -H "Authorization: Bearer $REPOWIRE_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"deploy <sha>","scope":"workers"}'
```

**RESOLVED (rz1g смержен, PR #37, sha 075d670 на origin/main):** endpoint реальный — `repowire/daemon/routes/control.py`, под `require_localhost + require_auth`. **Ответ: `{notified: int, target_epoch: str}`** (`notified` = число уведомлённых сессий). target_epoch daemon-derived через `compute_client_epoch` (`repowire/client_epoch.py`, version+mtime_ns). Хелпер `raise_for_status` (200) достаточен; рекомендуется дополнительно логировать `notified` из тела для операционной видимости деплоя. Ветка n8pt должна быть rebase'нута на origin/main (075d670), чтобы deploy-скрипт лежал рядом с реальным endpoint и PR мержился чисто.

---

## Empirical facts (devops-head собрал на железе, 2026-06-21)

Process landscape (`ps`, parent PID `1729` = tmux server):
- **Live MCP-клиент:** пара `ops-typed/bin/mcp-repowire-wrapper` → child `.../repowire mcp`.
- **Live WS-хук:** `.../repowire/hooks/websocket_hook.py`.
- **Daemon (НЕ трогать):** `.../repowire serve`.
- **Чужой MCP (НЕ трогать):** `python3 -m graphify.serve <...>`.

Маркеры в `/proc/<pid>/environ`:
- Live WS-хук: `REPOWIRE_PEER_ID=repow-project-agents-brain-team-f98bc9e6`, `REPOWIRE_TMUX_PANE=%328`, `TMUX=/tmp/tmux-.../workspace,...`, `REPOWIRE_CIRCLE=...`.
- **Orphan** (PID 1395918): `REPOWIRE_PEER_ID=repow-default-2bb40e47`, `REPOWIRE_DISPLAY_NAME=my-pro-claude-code`, **нет** `REPOWIRE_TMUX_PANE`, **нет** `TMUX` → панель мертва, circle `default` (старая сессия).

**Orphan-критерий (консервативный, И-условие):** процесс считается orphan **только если** (a) cmdline матчит `websocket_hook.py` ИЛИ ` repowire mcp` (не daemon, не graphify), И (b) у него есть `REPOWIRE_PEER_ID` (иначе не наш — skip), И (c) его `REPOWIRE_TMUX_PANE` отсутствует ИЛИ его нет в live-pane set от `tmux list-panes`, И (d) его `REPOWIRE_PEER_ID` отсутствует в live-peer set (status online) от `GET /peers`. Любая неопределённость → НЕ убивать.

tmux socket: `workspace` (`REPOWIRE_TMUX_SOCKET=workspace` в unit). Команда: `tmux -L workspace list-panes -a -F '#{pane_id}'`.

Deploy-окружение:
- uv tool: `~/.local/share/uv/tools/repowire/`, бинарь `~/.local/bin/repowire`.
- systemd: `systemctl --user restart repowire` (unit `~/.config/systemd/user/repowire.service`, `ExecStart=repowire serve`).
- Health: `GET http://127.0.0.1:8377/health`.

---

## File Structure

- `scripts/repowire_reap_orphans.py` — reaper: pure `find_orphans()` + gather/`main()`. Тестируемый.
- `scripts/repowire_deploy.sh` — атомарный deploy-оркестратор (bash, fail-fast).
- `scripts/repowire_refresh_clients.py` — Python-хелпер: POST /control/refresh-clients с auth (вызывается из deploy).
- `tests/test_reap_orphans.py` — unit-тесты pure-логики reaper.
- `tests/test_refresh_clients.py` — unit-тесты построения POST-запроса (auth header, body, non-200 → ошибка).
- `docs/runbook-repowire-deploy.md` — процедура деплоя + rollback + что делает каждый шаг.

Reaper и refresh-хелпер — pure-функции на входах-аргументах (DI), поэтому тестируются без живого daemon/ps/tmux.

---

## Task 1: Reaper — pure orphan-detection логика (TDD)

**Files:**
- Create: `scripts/repowire_reap_orphans.py`
- Test: `tests/test_reap_orphans.py`

- [ ] **Step 1: Failing test для `find_orphans`**

```python
# tests/test_reap_orphans.py
from scripts.repowire_reap_orphans import RepowireProc, find_orphans


def _proc(pid, kind, peer_id, pane):
    return RepowireProc(pid=pid, kind=kind, peer_id=peer_id, pane=pane)


def test_live_hook_with_live_pane_and_live_peer_is_not_orphan():
    procs = [_proc(100, "ws_hook", "repow-x", "%328")]
    orphans = find_orphans(procs, live_panes={"%328"}, live_peer_ids={"repow-x"})
    assert orphans == []


def test_hook_with_dead_pane_and_dead_peer_is_orphan():
    procs = [_proc(1395918, "ws_hook", "repow-default-2bb40e47", None)]
    orphans = find_orphans(procs, live_panes={"%328"}, live_peer_ids={"repow-x"})
    assert [o.pid for o in orphans] == [1395918]


def test_pane_alive_but_peer_dead_is_NOT_orphan_conservative():
    # И-условие: пока панель жива — не трогаем, даже если peer не в реестре
    procs = [_proc(200, "mcp", "repow-y", "%5")]
    orphans = find_orphans(procs, live_panes={"%5"}, live_peer_ids=set())
    assert orphans == []


def test_peer_alive_but_pane_dead_is_NOT_orphan_conservative():
    procs = [_proc(201, "mcp", "repow-z", "%999")]
    orphans = find_orphans(procs, live_panes=set(), live_peer_ids={"repow-z"})
    assert orphans == []


def test_proc_without_peer_id_is_skipped():
    procs = [_proc(202, "ws_hook", None, None)]
    orphans = find_orphans(procs, live_panes=set(), live_peer_ids=set())
    assert orphans == []
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd <repo> && python -m pytest tests/test_reap_orphans.py -v`
Expected: FAIL — `ModuleNotFoundError`/`ImportError: cannot import name 'find_orphans'`.

- [ ] **Step 3: Минимальная реализация pure-логики**

```python
# scripts/repowire_reap_orphans.py
"""Reaper of orphan repowire websocket_hook / mcp processes (beads-n8pt).

Orphan := repowire-managed process whose tmux pane is dead AND whose peer is
not in the daemon's live registry. Conservative: any ambiguity -> keep alive.
NEVER targets `repowire serve` (daemon) or `graphify.serve`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepowireProc:
    pid: int
    kind: str          # "ws_hook" | "mcp"
    peer_id: str | None
    pane: str | None    # REPOWIRE_TMUX_PANE, e.g. "%328"


def find_orphans(
    procs: list[RepowireProc],
    live_panes: set[str],
    live_peer_ids: set[str],
) -> list[RepowireProc]:
    """Return procs that are orphan under the conservative AND-rule.

    A proc is orphan iff it has a peer_id (ours) AND its pane is dead
    (missing or not in live_panes) AND its peer is not live.
    """
    orphans = []
    for p in procs:
        if not p.peer_id:
            continue  # not a repowire-managed session proc -> skip
        pane_dead = p.pane is None or p.pane not in live_panes
        peer_dead = p.peer_id not in live_peer_ids
        if pane_dead and peer_dead:
            orphans.append(p)
    return orphans
```

- [ ] **Step 4: Запустить тесты — зелёные**

Run: `cd <repo> && python -m pytest tests/test_reap_orphans.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/repowire_reap_orphans.py tests/test_reap_orphans.py
git commit -m "feat(reaper): conservative AND-rule orphan detection for repowire procs (beads-n8pt)"
```

---

## Task 2: Reaper — gather входов из ps/tmux/daemon + `main()` с dry-run

**Files:**
- Modify: `scripts/repowire_reap_orphans.py`
- Test: `tests/test_reap_orphans.py`

- [ ] **Step 1: Failing test для парсинга `ps` и env**

```python
# добавить в tests/test_reap_orphans.py
from scripts.repowire_reap_orphans import classify_cmdline, parse_environ


def test_classify_cmdline_distinguishes_kinds():
    assert classify_cmdline("/.../repowire/hooks/websocket_hook.py") == "ws_hook"
    assert classify_cmdline("/.../python /.../repowire mcp") == "mcp"
    assert classify_cmdline("/.../repowire serve") is None       # daemon — НЕ трогать
    assert classify_cmdline("python3 -m graphify.serve /...") is None  # чужой


def test_parse_environ_extracts_peer_and_pane():
    raw = "REPOWIRE_PEER_ID=repow-x\x00REPOWIRE_TMUX_PANE=%328\x00FOO=bar\x00"
    peer, pane = parse_environ(raw)
    assert peer == "repow-x"
    assert pane == "%328"


def test_parse_environ_missing_pane_returns_none():
    raw = "REPOWIRE_PEER_ID=repow-default\x00REPOWIRE_DISPLAY_NAME=my-pro\x00"
    peer, pane = parse_environ(raw)
    assert peer == "repow-default"
    assert pane is None
```

- [ ] **Step 2: Запустить — падает**

Run: `cd <repo> && python -m pytest tests/test_reap_orphans.py -v`
Expected: FAIL — `cannot import name 'classify_cmdline'`.

- [ ] **Step 3: Реализовать gather-функции + main**

```python
# добавить в scripts/repowire_reap_orphans.py
import argparse
import os
import signal
import subprocess
import sys
import time

import httpx

DAEMON_URL = os.environ.get("REPOWIRE_DAEMON_URL", "http://127.0.0.1:8377")
TMUX_SOCKET = os.environ.get("REPOWIRE_TMUX_SOCKET", "workspace")


def classify_cmdline(cmdline: str) -> str | None:
    if "repowire/hooks/websocket_hook.py" in cmdline:
        return "ws_hook"
    if cmdline.rstrip().endswith("repowire mcp") or "/repowire mcp" in cmdline:
        return "mcp"
    return None  # daemon (`repowire serve`), graphify, anything else -> skip


def parse_environ(raw: str) -> tuple[str | None, str | None]:
    env = {}
    for item in raw.split("\x00"):
        if "=" in item:
            k, _, v = item.partition("=")
            env[k] = v
    return env.get("REPOWIRE_PEER_ID"), env.get("REPOWIRE_TMUX_PANE")


def gather_procs() -> list[RepowireProc]:
    out = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=True
    ).stdout
    procs: list[RepowireProc] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, cmdline = line.partition(" ")
        kind = classify_cmdline(cmdline)
        if kind is None:
            continue
        try:
            pid = int(pid_str)
            raw = open(f"/proc/{pid}/environ").read()
        except (ValueError, OSError):
            continue  # gone / no perm -> skip
        peer, pane = parse_environ(raw)
        procs.append(RepowireProc(pid=pid, kind=kind, peer_id=peer, pane=pane))
    return procs


def gather_live_panes() -> set[str]:
    out = subprocess.run(
        ["tmux", "-L", TMUX_SOCKET, "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return {p.strip() for p in out.splitlines() if p.strip()}


def gather_live_peer_ids() -> set[str]:
    token = os.environ.get("REPOWIRE_AUTH_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.get(f"{DAEMON_URL}/peers", headers=headers, timeout=10.0)
    resp.raise_for_status()
    return {
        p["peer_id"]
        for p in resp.json()["peers"]
        if p.get("status") == "online"
    }


def reap(orphans: list[RepowireProc], apply: bool) -> None:
    for o in orphans:
        print(f"[orphan] pid={o.pid} kind={o.kind} peer={o.peer_id} pane={o.pane}")
        if not apply:
            continue
        try:
            os.kill(o.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    if apply and orphans:
        time.sleep(3)
        for o in orphans:
            try:
                os.kill(o.pid, 0)
                os.kill(o.pid, signal.SIGKILL)  # still alive -> hard kill
            except ProcessLookupError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reap orphan repowire ws_hook/mcp procs")
    ap.add_argument("--apply", action="store_true", help="actually kill (default: dry-run)")
    args = ap.parse_args(argv)
    procs = gather_procs()
    orphans = find_orphans(procs, gather_live_panes(), gather_live_peer_ids())
    if not orphans:
        print("no orphans found")
        return 0
    reap(orphans, apply=args.apply)
    print(f"{'killed' if args.apply else 'would kill'} {len(orphans)} orphan(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Запустить тесты — зелёные**

Run: `cd <repo> && python -m pytest tests/test_reap_orphans.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Реальный dry-run на железе (наблюдение, не правка)**

Run: `cd <repo> && python scripts/repowire_reap_orphans.py`
Expected: печатает 0+ orphan-строк (ожидаемо ловит PID 1395918 / circle `default`), завершает `0`, НИКОГО не убивает (dry-run). Сверить: в списке НЕТ `repowire serve` и НЕТ `graphify.serve`.

- [ ] **Step 6: Commit**

```bash
git add scripts/repowire_reap_orphans.py tests/test_reap_orphans.py
git commit -m "feat(reaper): gather ps/tmux/daemon inputs + dry-run main (beads-n8pt)"
```

---

## Task 3: Refresh-clients POST-хелпер (TDD, против контракта)

**Files:**
- Create: `scripts/repowire_refresh_clients.py`
- Test: `tests/test_refresh_clients.py`

- [ ] **Step 1: Failing test — построение запроса**

```python
# tests/test_refresh_clients.py
from scripts.repowire_refresh_clients import build_request


def test_build_request_with_token_sets_bearer():
    method, url, headers, body = build_request(
        daemon_url="http://127.0.0.1:8377",
        reason="deploy sha=abc",
        scope="workers",
        token="secret",
    )
    assert method == "POST"
    assert url == "http://127.0.0.1:8377/control/refresh-clients"
    assert headers["Authorization"] == "Bearer secret"
    assert body == {"reason": "deploy sha=abc", "scope": "workers"}  # no target_epoch (daemon-derived)


def test_build_request_without_token_omits_auth():
    _, _, headers, _ = build_request(
        daemon_url="http://127.0.0.1:8377", reason="r", scope="all", token=None
    )
    assert "Authorization" not in headers


def test_build_request_rejects_bad_scope():
    import pytest
    with pytest.raises(ValueError):
        build_request(daemon_url="http://x", reason="r", scope="everyone", token=None)
```

- [ ] **Step 2: Запустить — падает**

Run: `cd <repo> && python -m pytest tests/test_refresh_clients.py -v`
Expected: FAIL — `cannot import name 'build_request'`.

- [ ] **Step 3: Реализация**

```python
# scripts/repowire_refresh_clients.py
"""POST /control/refresh-clients helper for the deploy script (beads-n8pt).

Contract (frozen, beads-rz1g): POST {reason, scope[, target_epoch]} -> 200.
target_epoch omitted by default — daemon derives its own deployed-epoch
post-restart (ASSUMPTION-PENDING-USER-REVIEW, corr notif-cd28b280).
"""
from __future__ import annotations

import argparse
import sys

import httpx

_VALID_SCOPES = {"workers", "all", "advisory"}


def build_request(daemon_url: str, reason: str, scope: str, token: str | None):
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {_VALID_SCOPES}, got {scope!r}")
    url = f"{daemon_url.rstrip('/')}/control/refresh-clients"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"reason": reason, "scope": scope}
    return "POST", url, headers, body


def main(argv: list[str] | None = None) -> int:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon-url",
                    default=os.environ.get("REPOWIRE_DAEMON_URL", "http://127.0.0.1:8377"))
    ap.add_argument("--reason", required=True)
    ap.add_argument("--scope", default="workers")
    # Token from env $REPOWIRE_AUTH_TOKEN by default (backend-head notif-d800fdec).
    ap.add_argument("--token", default=os.environ.get("REPOWIRE_AUTH_TOKEN") or None)
    args = ap.parse_args(argv)
    method, url, headers, body = build_request(
        args.daemon_url, args.reason, args.scope, args.token
    )
    resp = httpx.request(method, url, headers=headers, json=body, timeout=30.0)
    resp.raise_for_status()  # non-200 -> deploy fails loudly
    print(f"refresh-clients OK: {resp.status_code} {resp.text[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Тесты зелёные**

Run: `cd <repo> && python -m pytest tests/test_refresh_clients.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/repowire_refresh_clients.py tests/test_refresh_clients.py
git commit -m "feat(deploy): refresh-clients POST helper against rz1g contract (beads-n8pt)"
```

---

## Task 4: Атомарный deploy-скрипт (bash, fail-fast)

**Files:**
- Create: `scripts/repowire_deploy.sh`

- [ ] **Step 1: Написать скрипт**

```bash
#!/usr/bin/env bash
# Atomic repowire-fork deploy (beads-n8pt):
#   reinstall -> restart daemon -> health-wait -> refresh-clients -> reap orphans
# Fail-fast: any stage failure aborts. Re-runnable (idempotent): refresh no-ops
# when daemon epoch unchanged; reaper is a no-op when nothing is orphan.
set -euo pipefail

REPO="${REPO:-$HOME/repos/agents-brain-team/repowire-fork}"
DAEMON_URL="${REPOWIRE_DAEMON_URL:-http://127.0.0.1:8377}"
SCOPE="${REFRESH_SCOPE:-workers}"
APPLY_REAP="${APPLY_REAP:-0}"   # 0 = dry-run reaper (default, safe)

log() { printf '[deploy %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

cd "$REPO"
SHA="$(git rev-parse --short HEAD)"
REASON="deploy repowire-fork sha=${SHA}"

# Capture currently installed version for rollback note.
PREV_VER="$(repowire --version 2>/dev/null || echo unknown)"
log "current installed version: ${PREV_VER}"

# 1) Reinstall from this checkout. NB: `uv tool install` does NOT accept the
#    TOML uv.lock as --constraints (uv.lock != pip constraints format; uv errors
#    "no such comparison operator =" — verified on host, beads-n8pt). Use the
#    canonical repo form (CLAUDE.md): full reinstall from the checkout.
log "uv tool reinstall from ${REPO}"
uv tool install --force --reinstall "${REPO}"

# 2) Restart daemon.
log "restart repowire.service"
systemctl --user restart repowire

# 3) Health-wait (fail-fast if daemon does not come up).
log "waiting for daemon health"
for i in $(seq 1 30); do
  if curl -fsS "${DAEMON_URL}/health" >/dev/null 2>&1; then
    log "daemon healthy after ${i}s"; break
  fi
  if [ "$i" -eq 30 ]; then
    log "ERROR: daemon did not become healthy in 30s — ABORT. Rollback: see docs/runbook-repowire-deploy.md"
    exit 1
  fi
  sleep 1
done

# 4) Refresh live clients (contract rz1g). Token from env $REPOWIRE_AUTH_TOKEN
#    (backend-head notif-d800fdec); helper reads it by default, header omitted
#    if unset. No config parsing.
log "POST /control/refresh-clients scope=${SCOPE}"
python3 "${REPO}/scripts/repowire_refresh_clients.py" \
  --daemon-url "${DAEMON_URL}" --reason "${REASON}" --scope "${SCOPE}"

# 5) Reap orphan ws_hook/mcp procs (dry-run unless APPLY_REAP=1).
log "reaping orphans (apply=${APPLY_REAP})"
if [ "${APPLY_REAP}" = "1" ]; then
  python3 "${REPO}/scripts/repowire_reap_orphans.py" --apply
else
  python3 "${REPO}/scripts/repowire_reap_orphans.py"
fi

log "deploy complete: sha=${SHA}"
```

- [ ] **Step 2: shellcheck чисто**

Run: `shellcheck scripts/repowire_deploy.sh`
Expected: no warnings (или только обоснованные info). Поправить, если есть.

- [ ] **Step 3: Commit**

```bash
chmod +x scripts/repowire_deploy.sh
git add scripts/repowire_deploy.sh
git commit -m "feat(deploy): atomic reinstall->restart->refresh->reap orchestrator (beads-n8pt)"
```

---

## Task 5: Runbook + rollback + финальная верификация

**Files:**
- Create: `docs/runbook-repowire-deploy.md`

- [ ] **Step 1: Написать runbook**

Содержание (Russian): назначение каждого шага deploy; команда запуска (`scripts/repowire_deploy.sh`, env-флаги `REFRESH_SCOPE`, `APPLY_REAP`); **rollback-план** (blast-radius: reinstall затрагивает живой daemon): при сбое health-wait или refresh — `uv tool install --reinstall repowire==<PREV_VER>` + `systemctl --user restart repowire`, проверить `/health`; reaper по умолчанию dry-run, kill только под `APPLY_REAP=1` после ручной сверки списка; идемпотентность: повторный запуск безопасен. Явно: координированный ВЫКАТ с respawn запускает **только director** в тихом окне после закрытия nkki — этот скрипт сам по себе не disruptive (refresh рефрешит на границе turn'а, mid-turn НЕ убивает).

- [ ] **Step 2: Прогнать весь тест-сьют**

Run: `cd <repo> && python -m pytest tests/test_reap_orphans.py tests/test_refresh_clients.py -v`
Expected: PASS (11 passed суммарно).

- [ ] **Step 3: Финальный реальный dry-run всего, КРОМЕ disruptive-шагов**

Только reaper dry-run + shellcheck (НЕ запускать reinstall/restart — это и есть выкат, его делает director):
Run: `python scripts/repowire_reap_orphans.py && shellcheck scripts/repowire_deploy.sh`
Expected: orphan-список напечатан, никто не убит; shellcheck чист.

- [ ] **Step 4: Commit + push + PR**

```bash
git add docs/runbook-repowire-deploy.md
git commit -m "docs(deploy): repowire deploy + rollback runbook (beads-n8pt)"
git push -u origin HEAD
gh pr create --title "feat(deploy): atomic deploy-time client-refresh + orphan-reaper (beads-n8pt)" \
  --body "$(cat <<'EOF'
## Summary
Atomic repowire-fork deploy: reinstall -> restart daemon -> health-wait -> POST /control/refresh-clients -> reap orphans. Plus standalone conservative orphan-reaper for ws_hook/mcp procs.

Implements beads-n8pt (devops slice of ii5m). Backend slice = beads-rz1g (control-endpoint + WS refresh). Contract frozen by backend-head.

## Contract notes
- `target_epoch` omitted from refresh POST — daemon-derived post-restart, CONFIRMED by backend-head (notif-d800fdec). Token from env `$REPOWIRE_AUTH_TOKEN`.
- Endpoint JSON response shape (notified-session count) finalized after rz1g worker commit; helper currently only asserts 200.

## Test plan
- `pytest tests/test_reap_orphans.py tests/test_refresh_clients.py` (11 passed)
- reaper dry-run on host catches PID-1395918-class orphans, skips daemon/graphify
- shellcheck clean

NB: this PR is non-disruptive wiring only. Coordinated rollout (with session respawn) is director-triggered in a quiet window after beads-nkki closes.
EOF
)"
```

---

## Self-Review

**Spec coverage:** (1) атомарный deploy reinstall→restart→refresh→reaper — Task 4 ✓. (2) reaper orphan ws_hook/mcp против live-pane set, скриптово, не ручной kill, покрывает PID 1395918 + будущие — Task 1+2 ✓. (3) синк по контракту перед реализацией refresh-вызова — Frozen contract (epoch+auth подтверждены backend-head notif-d800fdec) + Task 3 ✓. (4) идемпотентность повторного запуска — Task 4 (refresh no-op по epoch, reaper no-op) + runbook ✓. (5) тайминг: код сейчас, выкат — director — отражено в runbook/PR-body ✓.

**Placeholder scan:** код во всех code-шагах полный; единственный явный «свериться с репо» — Task 4 Step 3 (точная сигнатура `load_config`), оформлен как verify-шаг, не placeholder.

**Type consistency:** `RepowireProc(pid,kind,peer_id,pane)`, `find_orphans(procs, live_panes, live_peer_ids)`, `build_request(daemon_url, reason, scope, token)` — имена консистентны между Task 1/2/3 и их тестами.

**Open dependency:** end-to-end refresh-POST тестируется только после merge rz1g (endpoint ещё не существует). Reaper полностью независим и проверяется сразу. Это отражено в Task 3 (unit-тест строит запрос без живого endpoint) и PR-body.
