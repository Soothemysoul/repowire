# Design: фикс circle-мисрегистрации + hardening reaper/registry (beads-q2ok)

- **Дата:** 2026-06-15 (rev.2 — после live-инцидента и коррекции RCA)
- **Эпик:** beads-q2ok (P1, project=agents-brain-team)
- **Subtask'и:** beads-lyfk (A, circle, P1), beads-nxxm (B, reaper/registry, P2)
- **Репозиторий:** repowire-fork (демон + ws-hook)
- **Статус:** brainstorm продолжается; checkpoint-greenlight director'а — перед merge+рестартом

> Blast radius: правки в mesh-демоне, от которого зависят ВСЕ живые агенты.
> Merge + рестарт демона — только после greenlight director'а и координации
> окна с drafter-pm (#96→#95+wupw). Решение one-vs-two рестартов — в момент
> завершения drafter-мержей по готовности A+B.

---

## Incident addendum (2026-06-15, live) — что произошло и как чинили рантайм

Во время работы баг ударил по ЖИВОЙ сессии director (user-facing аутэйдж: ни
Telegram, ни пиры не достучаться). Диагностика дала **две коррекции к
первоначальному RCA** (оба симптома — класс registry-десинка, deliverable B/A):

### Симптом-инцидент: director залип status=offline при живом WS
- Запись `repow-global-80bbc633` (director): circle=global (ВЕРНЫЙ),
  status=offline, pane_id=null. MCP-канал жив (set_description работал).
- Его ws-hook (pid) имел **ESTABLISHED** WS-сокет к демону и сидел в
  message-loop, НО демон потерял его из `transport._connections` →
  `is_connected=False` → `liveness_tick` держал offline и не промоутил.
- **Корень:** стейл-disconnect гонка — запись снесена из
  `transport._connections` БЕЗ закрытия сокета. Обе стороны не детектят
  разрыв: ws-hook ждёт сообщений (TCP keepalive проходит на уровне
  протокола websockets), демон его не знает, reconnect не триггерится.
- **Рантайм-починка (без рестарта демона):** запустил свежий ws-hook с env
  director'а (TMUX_PANE, REPOWIRE_PEER_ID, REPOWIRE_CIRCLE=global) → connect
  по `peer_id` пошёл в **takeover-ветку** `allocate_and_register` (без
  singleton-проверки) → status=ONLINE + `transport.connect`. Старый
  застрявший ws-hook убит (guard по websocket-identity не дал ложного
  offline). director восстановлен и подтвердил whoami=online@global.

### Коррекция RCA deliverable A: clobber — это РЕГИСТРАЦИЯ в view-circle, не rename
- В логе демона (46MB): **НОЛЬ** POST'ов `/hooks/lifecycle/session-renamed`,
  ноль `Circle updated`/`session_renamed: moved`/`POST /peers/circle`.
  → Первоначальная гипотеза (clobber через after-rename-session hook +
  `handle_session_renamed`) **в этом инциденте не подтверждается**.
- Реальный путь: ws-hook при ПУСТОМ `REPOWIRE_CIRCLE` падает на
  `circle = get_tmux_info()['session_name']` (websocket_hook.py:513).
  `get_tmux_info` (_tmux.py:73) делает
  `tmux display-message -t <pane> '#{session_name}'`, который для pane в
  **grouped/linked** сессии возвращает имя VIEW-сессии. Проверено живьём:
  `display-message -t %64` (pane director'а) → `global-view-agents-brain-team`.
- Подтверждение: peer_id stale-записи
  `repow-global-view-agents-brain-team-e450f512` по ПРЕФИКСУ кодирует circle
  РЕГИСТРАЦИИ (формат `repow-<circle>-<hash>`). Та director-сессия
  спавнилась без `REPOWIRE_CIRCLE` → fallback на имя grouped-сессии →
  зарегистрировалась в view-circle. Это registration-time, не rename.

### Симптом-класс: singleton-conflict reconnect-storm
- Лог завален повторяющимся
  `WebSocket rejected (singleton conflict): Singleton role already online:
  qa-head-claude-code@project-agents-brain-team`.
- ws-hook qa-head в reconnect-loop отбивается ghost-записью «online»
  (status=ONLINE без живого транспорта). Singleton-проверка
  (websocket.py:158, peer_registry `allocate_and_register`) смотрит на
  status==ONLINE, а не на `transport.is_connected` → ghost блокирует
  реальный reconnect навсегда → деградация (CPU/лог).

### Применённый рантайм-стопгап (c)
- Снят tmux-hook `after-rename-session[42]` (belt-and-suspenders). Честно:
  в инциденте не срабатывал; реальная защита от clobber — наличие
  `REPOWIRE_CIRCLE`. Авто-восстановится при рестарте демона (install_hooks).

### Второй инцидент (тест-индуцированный MASS-десинк) + RELEASE-GATE
После рестарта-1 devops-worker гонял B-тесты (demote/disconnect/interrupt) по
ЖИВОМУ демону → one-shot MASS-десинк (director/drafter-pm/backend-head/
devops-head offline; placeholder `notif-aaaa…` AUTO-ACK в пейне director'а).
Восстановлено per-peer takeover'ом после kill воркера; рестарт-2 не
понадобился.
🔴 **RELEASE-GATE (non-negotiable):** тесты mesh-delivery (demote/disconnect/
interrupt/singleton/socket-close/liveness) — ТОЛЬКО против изолированного
инстанса демона (отдельный port/tmpdir) ИЛИ мок-transport/tmux; НИКОГДА
против живого `127.0.0.1:8377`. Защита в репе: autouse-фикстура
`tests/conftest.py::_isolate_daemon_target` (commit ba3f16e). Обязательный
acceptance-criterion PR-ревью; эмпирическая проверка изоляции на первом
B-тесте при возобновлении.

---

## Fix A — circle-мисрегистрация (beads-lyfk, P1)

### Реальная первопричина
Сессия, спавнящаяся без `REPOWIRE_CIRCLE`, регистрируется через ws-hook в
circle = имя grouped view-сессии (`global-view-<project>`), потому что
`display-message -t <pane>` резолвит pane в shared window-set к attached
view-сессии, а не к базовой `global`.

### Выбранный дизайн A (defense-in-depth, 3 слоя)
1. **Гарантировать `REPOWIRE_CIRCLE` для всех спавнов** (основной фикс).
   `spawn-claude` / `session_handler.py` ДОЛЖНЫ всегда экспортировать
   `REPOWIRE_CIRCLE` в окружение сессии. Если источник в system-репе
   (`spawn-claude`) — отдельный per-repo subtask (metadata.repo=system).
2. **Нормализация fallback в ws-hook** (websocket_hook.py:513): если
   `REPOWIRE_CIRCLE` пуст, fallback НЕ должен брать имя grouped/view-сессии
   как circle. Варианты: (а) резолвить базовую сессию группы
   (`#{session_group}` → канонический member, не view-alias); (б) маппить
   `global-view-*` → `global` через конфиг-паттерн; (в) при невозможности
   определить базовый circle — `default`, не view-имя.
3. **Защита на регистрации в демоне** (`allocate_and_register` /
   websocket.py connect): не принимать имена, матчащие
   `daemon.tmux.non_circle_session_patterns` (дефолт `["global-view-*"]`),
   как circle — нормализовать к базовому или отклонять.
4. **(вторично) rename-hook guard** — `handle_session_renamed` пропускает
   view-сессии (на случай, если rename-путь когда-либо активируется).

### DoD A
- Сессия, спавнящаяся в `global` без явного circle, регистрируется в
  `global`, а НЕ в `global-view-*`.
- Юнит: ws-hook fallback при пустом REPOWIRE_CIRCLE + pane в grouped-сессии
  → circle нормализован к `global`, не `global-view-*`.
- Юнит: демон отклоняет/нормализует регистрацию с circle=`global-view-*`.
- Tilix two-pane UI (linked-сессии) не сломан.

---

## Fix B — hardening reaper/registry: весь класс десинка (beads-nxxm, P2)

Директор: B должен лечить ВЕСЬ класс, не один симптом. Три подтверждённых
механизма десинка:

### B-1: стейл-disconnect гонка (zombie half-open) — корень director-инцидента
Запись сносится из `transport._connections` без закрытия сокета → ws-hook
залипает в message-loop, демон его не знает, reconnect не триггерится.
**Фикс:** при любом удалении соединения (`transport.disconnect`,
`mark_offline`, ghost-демоут) — **закрывать underlying websocket**, чтобы
клиентский reconnect-loop сработал. Доп.: ws-hook периодически
верифицирует, что он ещё в реестре (app-level heartbeat «registered?», а не
только TCP keepalive); при «not registered» — реконнект.

### B-2: singleton-conflict reconnect-storm (ghost online) — корень qa-head storm
Singleton-проверка смотрит на `status==ONLINE`, не на живой транспорт →
ghost-запись блокирует reconnect реального singleton-peer'а.
**Фикс:** singleton-проверка (websocket.py:158, `allocate_and_register`)
должна считать «занято» ТОЛЬКО если держатель `transport.is_connected`.
Ghost (online без WS) → разрешить takeover / демоутить ghost перед проверкой.

### B-3: busy-зомби + отсутствие pane-liveness на таймере (исходный B)
- `busy` снимается только Stop-хуком; pane умер mid-turn → busy навсегда.
- `liveness_tick` (5с) сверяет только WS-коннект; pane-ping sweep
  (`_demote_unsafe_connected_peers`) только лениво (HTTP, debounce 30с).
**Фикс:** периодический pane-liveness sweep (новый цикл ~30с) переиспользует
`_demote_unsafe_connected_peers`; поле `Peer.busy_since` (телеметрия +
триггер пинга, НЕ критерий эвикции — не убиваем живой долгий turn);
reaping осиротевших ws-hook (сверка `pane_id` с `tmux list-panes`).

### DoD B
- B-1: запись, снятая из `_connections`, закрывает сокет → клиент
  реконнектится → online за ≤ reconnect-интервал, без ручного relaunch.
- B-2: ghost online-запись (без транспорта) НЕ блокирует reconnect
  singleton-peer'а; storm не воспроизводится.
- B-3: busy-зомби после смерти панели уходит offline за ≤ sweep-интервал.
- Юнит/интеграционные тесты на каждый из B-1/B-2/B-3.

---

## Декомпозиция по репам
- beads-lyfk (A) + beads-nxxm (B) → **repowire-fork** (демон + ws-hook).
- Слой A-1 (гарантия REPOWIRE_CIRCLE в `spawn-claude`) может затронуть
  **system** — если так, отдельный per-repo subtask (metadata.repo=system).
- twwz (gsd-dev 403) — отдельный слой (allowed_commands), едет в том же
  restart-окне без brainstorm.

## План отгрузки/рестарта (по решению director'а)
- Порядок: рестарт ТОЛЬКО после drafter-pm #96→#95+wupw. go/no-go +
  предупреждение юзера — за director'ом; devops-head предлагает окно.
- one-vs-two рестартов — решаем в момент завершения drafter-мержей по
  готовности A+B. Если A+B готовы → один рестарт (state-rebuild + деплой).
  Если нет → state-rebuild рестарт тогда же (снимет storm+десинк+зомби),
  деплой A+B вторым окном.
- Как только A+B имплементационно готовы — пинг director'у для бандлинга.
- Мониторинг qa-head storm: если деградирует демон до drafter-мержей —
  эскалация (director пересмотрит приоритет окна).

## Открытые вопросы к director (checkpoint, перед имплементацией кодом)
1. A-слой нормализации: маппинг `global-view-*`→`global` по паттерну
   (просто) vs резолв базовой сессии группы через `#{session_group}`
   (общее, но сложнее)?
2. B-1: закрывать сокет на стороне демона при disconnect — ОК как основной
   механизм, или предпочитаешь ws-hook app-heartbeat?
3. Запускать имплементацию через devops-worker сейчас (PR откроется, merge
   ждёт greenlight + restart-окно), или сперва твой ОК по этому ревизованному
   дизайну?
