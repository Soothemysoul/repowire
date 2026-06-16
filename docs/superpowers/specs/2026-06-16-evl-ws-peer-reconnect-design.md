# beads-evl — Peer-side WS reconnect resilience + pane-warning (Fix A + Fix C)

- **Issue:** beads-evl (P1, bug) — «repowire WS-vs-pane state divergence: peer alive but WS dead → orchestration deadzone».
- **Repo:** `repowire-fork` (single-repo).
- **Предшественник:** beads-q2ok / beads-nxxm (Deliverable B) — daemon-side reaping уже в проде (`docs/superpowers/specs/2026-06-15-q2ok-circle-reaper-fix-design.md`).
- **Статус:** design (brainstorm), ожидает финального ревью director перед writing-plans.

---

## 1. Проблема и уточнённый RCA

Симптом: pane жив + процесс Claude работает + repowire WebSocket мёртв. `list_peers` показывает `offline`, `notify_peer`/`ask_peer` → 503. Director принимает peer за мёртвого, берёт оркестрацию на себя / спавнит дубль → orchestration deadzone + дублирование работы.

Чтение исходников (`repowire/hooks/websocket_hook.py`, `repowire/hooks/session_handler.py`, `repowire/daemon/websocket_transport.py`, `repowire/daemon/peer_registry.py`) дало уточнённый корень:

1. **Peer-side reconnect УЖЕ существует**, но **bounded**. `websocket_hook.py:main()` — `while attempt < max_attempts` (`max_attempts = 50`), exp backoff с cap 5s, re-handshake через `connect_msg` (восстанавливает регистрацию, peer_id reuse). При исчерпании 50 попыток `main()` делает `return 1` → процесс hook'а **умирает навсегда**.

2. **Hook запускается один раз** — `session_handler.py` (SessionStart hook) стартует `websocket_hook.py` как detached `subprocess.Popen` (one-per-pane, flock-singleton). После exit'а hook'а ничто его не перезапускает до следующего SessionStart, а тот срабатывает только на **новой** сессии Claude, не в середине. Итог: при rate-limit cascade / daemon-down окне длиннее ~100–250s (50 × 2–5s) hook исчерпывает попытки, умирает, и pane остаётся **без ws-hook навсегда** — вечный deadzone.

3. **Half-open зомби** (гонка `transport._connections` без закрытия сокета) **уже лечится** daemon-side: `unsafe_sweep_loop` (`_demote_unsafe_connected_peers`, интервал 30s, ping timeout 1s) + q2ok `WebSocketTransport.disconnect()`, который закрывает сокет вне lock → форсирует peer-side reconnect. `liveness_tick` (5s) half-open НЕ ловит (`is_connected()==True`, сокет не закрыт) — но sweep ловит. **Вывод:** конкретный half-open кейс покрыт sweep'ом, **но только пока hook-процесс жив и способен переподключиться**. Если hook умер по п.1–2, sweep корректно пометит `offline`, но воскрешать некому.

**Ключевой сдвиг:** задача Fix A — не «добавить reconnect» (он есть), а **«hook не должен умирать навсегда, пока pane жив»**.

---

## 2. Что уже в проде — НЕ трогаем

- Daemon-side health probing (Fix B): `liveness_tick_loop` (5s), `unsafe_sweep_loop` (`unsafe_sweep_interval_sec`, default 30s).
- q2ok half-open fix: `WebSocketTransport.disconnect()` закрывает сокет вне lock.
- Daemon-side false-offline detection (частичный Fix D) через pane-ping sweep.
- peer_id reuse по identity `(path, circle, backend)` — `_try_reuse_by_identity_unlocked`.

Этот spec **не меняет** daemon-side. Вся работа — peer-side (`websocket_hook.py`) + launch-обвязка + тесты + wiki-doc.

---

## 3. Scope этого spec

- **Fix A** — peer-side WS resilience: unbounded reconnect + watchdog defense-in-depth + intentional-shutdown guard.
- **Fix C** — видимое предупреждение в pane при потере WS (только tmux display, без stdin-инъекции).
- **Regression-тесты** на WS-drop/recover (включая реальный forced fault).
- **wiki recovery-doc** (через capture pipeline, не прямой write в brain).

Вне scope — §9.

---

## 4. Архитектура / компоненты

### 4.1. Fix A.1 — unbounded reconnect в `websocket_hook.py:main()`

**Изменение:** убрать `max_attempts = 50` hard-cap и exit-on-exhaust. Пока pane жив, hook переподключается бесконечно.

**Capped exponential backoff + jitter** (требование director — против reconnect-storm / thundering-herd при возврате daemon после длинного окна; тот же класс, что q2ok singleton-conflict):

- Базовый backoff растёт экспоненциально, capped (предлагаю cap = 30s; вынести в env-настройку для тестируемости — см. §7).
- **Full jitter:** фактическая задержка = `random.uniform(0, min(cap, base * 2**attempt))`. Разносит одновременный возврат множества peer'ов по времени.
- `attempt` сбрасывается в 0 при успешном `connected`.

**Guard «pane жив vs pane уходит»** (главный, marker-free): между попытками reconnect hook проверяет `_is_pane_safe(pane_id)` (уже есть — сверяет `pane_current_command` с `_expected_command`, снятым на старте).
- pane safe (Claude-процесс жив) → продолжать reconnect.
- pane unsafe (Claude ушёл — намеренный shutdown / restart / краш / reuse pane другим агентом) → `clear_pane_runtime_state` + `return 0`, не воскрешать.

Это естественно различает «WS упал, Claude жив» (reconnect вечно) и «Claude ушёл» (стоп), не завися от маркеров. На реальном intentional shutdown `agent-stop` гасит systemd-scope → SIGTERM убивает и Claude, и сам hook-процесс, поэтому в типичном пути guard даже не понадобится — но он защищает от случая, когда Claude вышел, а scope ещё не снесён.

### 4.2. Fix A.2 — watchdog defense-in-depth (supervise-обёртка)

Unbounded retry закрывает 99% (пока hook-процесс жив, он не сдаётся). Остаётся узкий риск: **hook-процесс умирает по необработанному исключению** вне `try` в `main()` → `asyncio.run` пробрасывает → процесс выходит, и до следующего SessionStart pane снова без ws-hook.

**Решение (лёгкое, в том же файле, без нового процесса/systemd-unit):** outer supervise-loop в `__main__` (или функция `supervise()`), которая повторно входит в `main()` при неожиданном возврате/исключении, **гейтированная двумя условиями**:

1. `_is_pane_safe(pane_id)` == True (Claude ещё жив) — иначе выходим.
2. **intentional-marker peek** — fresh `.shutdown-intentional` или `.restart-intentional` в `$HOME/ai-infra/ops/<role>/` отсутствует. **PEEK-only (stat без unlink)** — маркер one-shot consumed `agent-gateway.monitor_loop`; hook НЕ должен его потреблять, только подсмотреть существование+свежесть (порог 300s, как `INTENTIONAL_SHUTDOWN_MARKER_MAX_AGE_SEC`). Свежий маркер → намеренный shutdown/restart → watchdog НЕ воскрешает, выходит.

Между перезапусками — короткий backoff с jitter (тот же helper, что 4.1).

**Требование к окружению:** watchdog'у нужен путь к marker-dir, т.е. роль агента из env. Проверить, что `spawn-claude` экспортирует роль (кандидаты: `BRAIN_AGENT_ROLE` / `REPOWIRE_PEER_ROLE` / `REPOWIRE_AGENT_ROLE`) в env hook-процесса. Если надёжного env нет — это блокер дизайна, эскалировать (добавить экспорт в spawn-claude — отдельный мелкий правка в `system/`, межрепозиторно). Worker подтверждает на этапе TDD.

### 4.3. Fix C — видимое предупреждение в pane

При **устойчивой** потере WS (после grace-порога, чтобы не мигать на мгновенных блипах — предлагаю warn после `attempt >= 3` ИЛИ disconnect длится > ~10s) hook сигналит пользователю **без stdin-инъекции** (stdin-инъекция в активный turn = класс intent-mismatch/interrupt-багов, ломает работающий turn Claude — запрещено):

- **Персистентный индикатор** — `tmux select-pane -t <pane> -T "⚠ repowire WS lost"` (pane title) и/или `tmux set-option -t <pane> @repowire_ws_status lost` (user-option, рендерится в status-line, если тема его показывает). Снимается при успешном reconnect (title → исходный, option → ok/unset).
- **Одноразовый transient алерт** — `tmux display-message -t <pane> "repowire: WS соединение потеряно, переподключаюсь…"` при первом пересечении grace-порога (показывается в status-line несколько секунд, не трогает буфер ввода).
- **НЕ использовать** `display-popup` (модальный, крадёт фокус), `send-keys`, `paste-buffer`.

Точная комбинация (title vs user-option) — за worker'ом по факту того, что рендерит текущая tmux-тема; минимально обязателен `display-message` transient + снятие индикатора на recover.

---

## 5. Data flow / взаимодействие

```
SessionStart hook (session_handler.py)
  └─ Popen(websocket_hook.py)  ← detached, one-per-pane, flock
        └─ supervise()  [Fix A.2]
             loop:
               main()  [Fix A.1]
                 while True:                      ← unbounded (было: attempt<50)
                   connect → connected → message-loop
                   on drop:
                     if not _is_pane_safe: return 0   ← Claude ушёл → стоп
                     if attempt>=grace: pane-warn     ← Fix C
                     sleep(full_jitter(cap, attempt)) ← capped backoff+jitter
                 on reconnect: clear pane-warn, attempt=0
               main() вернулся/упал:
                 if not _is_pane_safe(): exit         ← guard 1
                 if intentional_marker_fresh(): exit  ← guard 2 (peek-only)
                 sleep(backoff); продолжить
```

Daemon-side (без изменений): sweep пингует, на отсутствие pong → `mark_offline` + `disconnect()` (закрывает сокет) → peer-side message-loop получает `ConnectionClosed` → reconnect.

---

## 6. Error handling / edge cases

- **Daemon-down длиннее старого 50-cap окна** (>250s): hook продолжает reconnect с capped backoff, переживает, восстанавливает регистрацию по возврату daemon (peer_id reuse). Это сам класс бага.
- **Reconnect-storm** (много peer'ов одновременно при возврате daemon): full jitter разносит во времени.
- **Intentional shutdown** (`agent-stop`): scope teardown SIGTERM убивает hook вместе со scope; если узкая гонка (Claude вышел, scope ещё жив) — guard pane-safety + marker-peek останавливают воскрешение.
- **Intentional restart** (`agent-stop AGENT_RESTART=1`, `.restart-intentional`): тот же marker-peek → watchdog не воскрешает; новый ws-hook поднимется штатно на новом SessionStart.
- **Pane reuse другим агентом**: `_is_pane_safe` сверяет `_expected_command` → unsafe → exit (уже есть, сохраняем).
- **Маркер потреблён gateway между peek и exit**: гонки нет — peek не потребляет; решение «выходить» принимается по свежести на момент peek, что корректно (намерение было).
- **tmux недоступен / pane закрылся при попытке warn**: Fix C best-effort, `subprocess` ошибки глотаются, не валят reconnect-loop.

---

## 7. Тестирование (DoD verification)

TDD, worker пишет тесты до имплементации, по существующим паттернам `repowire-fork/tests/`.

**Unit:**
- backoff: вычисляет capped + jittered задержки; верхняя граница ≤ cap; jitter в `[0, ceil]`.
- unbounded: цикл не выходит после 50 неуспехов (было: выходил).
- pane-safety guard: `_is_pane_safe`→False между попытками → `return 0`.
- watchdog: при возврате `main()` с safe pane и без свежего маркера → повторный вход; со свежим `.shutdown-intentional`/`.restart-intentional` (peek) → exit; маркер НЕ удалён после peek.
- Fix C: пересечение grace-порога → вызовы `tmux display-message` / `select-pane -T`; recover → снятие индикатора.

**Regression — реальный forced fault (требование director, не только мок):**
- Поднять реальный daemon + подключить hook; **убить процесс daemon** (socket-kill, не мок WS-drop); подождать окно, которое при тестовом backoff заведомо превышает 50 попыток; поднять daemon; **assert:** hook переподключился и восстановил регистрацию (peer_id reuse, `is_connected`/`list_peers` снова online).
- Чтобы доказать «бьёт старый 50-cap» без реальных 250s — **backoff cap вынесен в env** (напр. `REPOWIRE_WS_RECONNECT_CAP_SEC`), тест ставит малый cap и держит окно > (50 × тестовый backoff). Доказывает unbounded при компрессированном времени, оставаясь реальным socket-kill, не моком.

**Acceptance / soak (кандидат на отдельный independent qa-прогон):** литеральный iptables-drop + daemon-down >250s на дефолтном backoff. Director предложил завести qa-head subtask на независимый network-fault прогон, если сочту нужным — **рекомендую** именно для этого literal-soak, чтобы devops-worker не блокировался на 250s wall-clock в CI.

---

## 8. wiki recovery-doc

Через capture pipeline (НЕ прямой write в brain — Single-Writer Principle). После merge опишу вслух для librarian: как распознать состояние (pane жив, `list_peers` offline, ws-hook лог показывает reconnect-loop или процесс отсутствует) и процедуру ручного восстановления (проверить ws-hook pid/лог; при отсутствии процесса — рестарт сессии Claude; при наличии reconnect-loop — проверить daemon). Назначение wiki: `wiki/operations/`.

---

## 9. Вне scope

- Замена архитектуры daemon (инкрементально).
- Cross-machine WS reliability (single-machine деплой).
- Обработка самих Anthropic API rate-limit (отдельная тема).
- Изменения daemon-side health probing — уже в проде (§2).
- Новый systemd-unit под ws-hook — намеренно избегаем (watchdog в том же процессе, §4.2).

---

## 10. Открытые вопросы / риски

1. **Env с ролью для marker-path** (§4.2): подтвердить, что hook-процесс видит роль агента. Если нет — мелкая межрепозиторная правка в `spawn-claude` (`system/`). Worker проверяет первым шагом TDD; если блокер — эскалация ко мне (devops-head) → director.
2. **tmux user-option vs pane-title для Fix C**: зависит от текущей tmux-темы; worker выбирает по факту, минимум — `display-message` + снятие на recover.
3. **Literal >250s soak** — выносится в опциональный independent qa-прогон, не в CI devops-worker (см. §7).
