# Runbook: атомарный деплой repowire-fork (deploy-time client-refresh + orphan-reaper)

> beads-n8pt (devops-срез ii5m). Backend-срез — beads-rz1g (control-endpoint
> `POST /control/refresh-clients` + WS-`refresh` + handshake epoch). Контракт
> заморожен backend-head.

## Зачем

MCP-клиент repowire грузит код пакета **per-session**. Рестарт daemon сам по
себе не обновляет уже живые сессии — они продолжают крутить старый код хука/MCP.
Этот деплой выкатывает новый код **и** рефрешит живые сессии, не теряя работу,
плюс вычищает orphan-процессы (websocket_hook/mcp), у которых умерла tmux-панель.

## Артефакты

| Файл | Роль |
|---|---|
| `scripts/repowire_deploy.sh` | Атомарный оркестратор (bash, fail-fast). |
| `scripts/repowire_refresh_clients.py` | POST `/control/refresh-clients` с auth (вызывается из deploy). |
| `scripts/repowire_reap_orphans.py` | Reaper orphan ws_hook/mcp процессов (по умолчанию dry-run). |

## Шаги деплоя (что делает скрипт)

`scripts/repowire_deploy.sh` выполняет АТОМАРНО и fail-fast (любой сбой — abort):

1. **`uv tool install --force --reinstall "${REPO}"`** — переустановка пакета из
   чекаута. `--force` перетирает существующий tool-env, `--reinstall` пересобирает
   зависимости (канонический форма из `CLAUDE.md`).
   *Примечание:* `uv.lock` — это TOML-локфайл uv, а **не** pip-constraints файл,
   поэтому его нельзя передать в `--constraints` (uv падает на парсинге). Деплой
   ставит из `pyproject.toml`/чекаута.
2. **`systemctl --user restart repowire`** — рестарт daemon (unit
   `~/.config/systemd/user/repowire.service`, `ExecStart=repowire serve`).
3. **Health-wait** — до 30 с опрашивает `GET ${DAEMON_URL}/health`; если daemon
   не поднялся — ABORT (см. rollback).
4. **`POST /control/refresh-clients`** (контракт rz1g) — daemon рассылает живым
   сессиям WS-`refresh`. Тело: `{reason, scope}`; `target_epoch` **не шлём** —
   daemon после рестарта подставляет свой deployed-epoch сам (CONFIRMED
   backend-head, notif-d800fdec). Токен — из env `$REPOWIRE_AUTH_TOKEN` (header
   опускается, если env пуст; конфиг не парсится).
5. **Reaper** — `scripts/repowire_reap_orphans.py`. По умолчанию **dry-run**
   (только печатает кандидатов); kill только при `APPLY_REAP=1`.

## Запуск

```bash
# Стандартный выкат (reaper в dry-run, scope=workers):
scripts/repowire_deploy.sh

# Параметры через env:
REFRESH_SCOPE=all   scripts/repowire_deploy.sh    # рефрешить все сессии, не только workers
APPLY_REAP=1        scripts/repowire_deploy.sh    # реально убить orphan-процессы
REPOWIRE_DAEMON_URL=http://127.0.0.1:8377 scripts/repowire_deploy.sh
REPO=/path/to/checkout scripts/repowire_deploy.sh
```

`scope` ∈ `workers` | `all` | `advisory` (контракт rz1g).

### Reaper отдельно (диагностика без деплоя)

```bash
python scripts/repowire_reap_orphans.py            # dry-run: печатает orphan-кандидатов
python scripts/repowire_reap_orphans.py --apply    # реально SIGTERM→(3с)→SIGKILL
```

Orphan-критерий **консервативный** (И-условие): процесс убивается только если
(a) cmdline = `websocket_hook.py` ИЛИ `repowire mcp` (НЕ daemon, НЕ graphify),
И (b) есть `REPOWIRE_PEER_ID`, И (c) tmux-панель мертва (нет `REPOWIRE_TMUX_PANE`
или её нет в `tmux -L workspace list-panes`), И (d) peer не online в `GET /peers`.
Любая неопределённость → **не убивать**.

## Rollback

**Blast-radius:** шаг 1 (reinstall) затрагивает живой daemon — после него идёт
рестарт. Если выкат сломался:

- **Сбой health-wait или refresh-POST** → откатить пакет на предыдущую версию и
  перезапустить daemon:
  ```bash
  uv tool install --force --reinstall repowire==<PREV_VER>   # PREV_VER из лога деплоя
  systemctl --user restart repowire
  curl -fsS http://127.0.0.1:8377/health                     # проверить, что поднялся
  ```
  Скрипт печатает `current installed version: <PREV_VER>` в начале — взять оттуда.
- **Reaper** по умолчанию dry-run — он ничего не ломает. Перед `APPLY_REAP=1`
  всегда сверять напечатанный список вручную (особенно `peer`/`pane`).

## Идемпотентность

Повторный запуск безопасен: refresh-POST — no-op, если epoch daemon не
изменился; reaper — no-op, если orphan-кандидатов нет. reinstall переустановит
ту же версию.

## Тайминг и кто запускает (HARD)

Этот скрипт — **проводка/код**, не disruptive сам по себе: refresh рефрешит
сессии на границе turn'а (mid-turn НЕ убивает), reaper по умолчанию dry-run.
**Координированный ВЫКАТ с respawn запускает только director** в тихом окне
ПОСЛЕ закрытия beads-nkki. Worker/head **не запускают** disruptive-шаги
(`uv tool reinstall`, `systemctl restart`, `APPLY_REAP=1`) — только pytest,
reaper `--dry-run`, shellcheck.

## Зависимость

End-to-end `POST /control/refresh-clients` проверяется только после merge
beads-rz1g (endpoint реализует backend; на момент написания не смержен).
Refresh-helper закодирован строго против замороженного контракта; unit-тесты
(`tests/test_refresh_clients.py`) строят запрос без живого endpoint — этого
достаточно для PR. Reaper полностью независим от rz1g и проверяется сразу.
