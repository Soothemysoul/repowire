# Implementation Plan: circle-мисрегистрация + reaper/registry hardening (beads-q2ok)

- **Spec:** `docs/superpowers/specs/2026-06-15-q2ok-circle-reaper-fix-design.md` (rev.2)
- **Эпик:** beads-q2ok | **Subtask'и:** beads-lyfk (A, P1), beads-nxxm (B, P2)
- **Ветка/worktree:** `fix/q2ok-circle-reaper` (от origin/main)
- **Репозиторий:** repowire-fork (всё ниже — в этой репе)
- **Режим:** TDD (тест-первый на каждую задачу). High blast-radius демона — без «while I'm here» правок, один фикс за раз.

> Порядок отгрузки: A (P1, корень аутэйджа) → B (P2). PR держать ОТКРЫТЫМ
> до greenlight director'а; merge+рестарт-2 — по его команде, координированно
> с drafter #96→#95+wupw. Если B затянется — A отгрузить первым PR.

---

## ФАЗА A — circle-мисрегистрация (beads-lyfk)

Реальная причина (исправленный RCA): ws-hook при пустом `REPOWIRE_CIRCLE`
падает на `circle = get_tmux_info()['session_name']`, который для pane в
grouped/linked-сессии возвращает имя view-сессии (`global-view-*`) →
регистрация в неверный circle. Доказано: 0 POST'ов rename-hook в логе;
`display-message -t %64` → `global-view-agents-brain-team`; peer_id-префикс
stale-записи = circle регистрации.

### A-task-1: нормализация fallback circle в ws-hook (основной фикс)
- **Файл:** `repowire/hooks/websocket_hook.py:513` (+ helper в `hooks/_tmux.py`).
- **Тест-первый:** юнит на хелпер нормализации circle:
  - вход `session_name="global-view-agents-brain-team"` → `"global"`;
  - вход `session_name="global-view-zeon"` → `"global"`;
  - вход `session_name="project-drafter"` → `"project-drafter"` (без изменений);
  - вход `session_name="global"` → `"global"`.
- **Реализация:** при пустом `REPOWIRE_CIRCLE` fallback не берёт view-имя
  сырьём. Предпочтительно — резолв базовой сессии группы:
  `tmux display-message -t <pane> '#{session_group}'` → канонический base
  member группы; если недоступно — нормализация по паттерну
  `global-view-<x>` → `global` / общий `<base>-view-<x>` → `<base>`.
  Паттерн вынести в константу/конфиг (см. A-task-3 reuse).
- **DoD:** при пустом REPOWIRE_CIRCLE и pane в grouped-сессии ws-hook
  регистрируется в base circle, не view.

### A-task-2: guard на регистрации в демоне (defense-in-depth)
- **Файлы:** `repowire/daemon/routes/websocket.py` (connect handler, ~L80-160),
  `repowire/daemon/peer_registry.py:allocate_and_register`,
  `repowire/config/models.py` (новое поле).
- **Тест-первый:** юнит — connect/allocate_and_register с
  circle=`global-view-agents-brain-team` → circle нормализуется к `global`
  (или регистрация отклоняется с понятной ошибкой, по выбору — нормализация
  предпочтительнее, не рвёт сессию).
- **Реализация:** конфиг-поле `daemon.tmux.non_circle_session_patterns:
  list[str]` (дефолт `["global-view-*"]`). В connect-хендлере/allocate
  нормализовать circle, матчащий паттерн, к base, ДО регистрации.
- **DoD:** демон никогда не хранит peer в circle `global-view-*`.

### A-task-3: rename-hook guard (вторичный дефенс)
- **Файл:** `repowire/daemon/lifecycle_handler.py:handle_session_renamed`.
- **Тест-первый:** peer в circle `global`, прилетает rename
  `new_name="global-view-agents-brain-team"` с его pane → circle НЕ меняется;
  контроль: легитимный rename базовой сессии → меняется.
- **Реализация:** в начале `handle_session_renamed` — если `new_name` матчит
  `non_circle_session_patterns`, ранний выход без `set_peer_circle`.
- **DoD:** rename view-сессии не клоббит circle.

### A-task-4 (investigation): пробел REPOWIRE_CIRCLE на спавн-путях
- spawn-claude (system, main.go:71) УЖЕ ставит REPOWIRE_CIRCLE — основной путь
  ок. Проверить НЕ-spawn-claude пути (session_handler.py /spawn, респавн
  ws-hook при compaction/restart сессии) — есть ли путь без env.
- **Если найден реальный пробел в system-репе** → НЕ чинить здесь;
  эскалировать devops-head, создаст per-repo subtask (metadata.repo=system).
  Если пробел в repowire-fork session_handler — добавить задачу A-task-5 в эту
  ветку.
- A-task-1/2 делают баг невозможным даже при пропущенном env — это основная
  защита; A-task-4 — дополнительная гигиена.

---

## ФАЗА B — reaper/registry hardening, весь класс (beads-nxxm)

### B-1: закрытие сокета при удалении из transport._connections (корень zombie)
- **Файлы:** `repowire/daemon/websocket_transport.py` (disconnect),
  `repowire/daemon/peer_registry.py` (mark_offline / ghost-демоут пути),
  возможно `routes/websocket.py` finally.
- **Корень:** запись сносится из `_connections` без закрытия websocket →
  ws-hook залипает в message-loop, демон не знает, reconnect не триггерится.
- **Тест-первый:** при удалении соединения из `_connections` (disconnect/
  mark_offline) — underlying websocket ЗАКРЫВАЕТСЯ (mock ws.close вызван).
- **Реализация:** в `transport.disconnect` (и путях, что демоутят
  connected-peer'а) — закрывать websocket (`await ws.close()`), чтобы
  клиентский reconnect-loop сработал. Доп. (опц.): ws-hook app-level
  «registered?» heartbeat — но основной механизм серверный (close).
- **DoD:** peer, чья запись снята из `_connections`, получает закрытие сокета
  → клиент реконнектится → online без ручного relaunch. Юнит + интеграционный
  (поднять ws-hook против тест-демона, снести запись, проверить reconnect).

### B-2: singleton-ghost check (корень reconnect-storm)
- **Файлы:** `repowire/daemon/routes/websocket.py:~158` (singleton reject),
  `repowire/daemon/peer_registry.py:_is_singleton_role` / allocate_and_register
  singleton-проверка (~L304, L351).
- **Корень:** singleton-проверка смотрит на `status==ONLINE`, не на живой
  транспорт → ghost (online без WS) блокирует reconnect реального peer'а →
  storm (10k+ строк в инциденте).
- **Тест-первый:** singleton-роль с существующей записью status=ONLINE, но
  `transport.is_connected==False` (ghost) → новый connect ДОПУСКАЕТСЯ
  (takeover/демоут ghost); контроль: держатель реально connected → reject.
- **Реализация:** singleton «занято» = держатель И status==ONLINE И
  `transport.is_connected(holder)`. Ghost → демоутить/разрешить takeover до
  проверки.
- **DoD:** ghost online-запись НЕ блокирует reconnect; storm не
  воспроизводится. Юнит.

### B-3: busy-зомби + периодический pane-liveness sweep
- **Файлы:** `repowire/protocol/peers.py` (поле `busy_since`),
  `repowire/daemon/peer_registry.py` (`_set_peer_status`, новый цикл),
  `repowire/daemon/app.py` (запуск цикла), `repowire/config/models.py`.
- **Тест-первый:**
  - `_set_peer_status` → BUSY ставит `busy_since`, выход из BUSY сбрасывает;
  - периодический sweep: peer BUSY + ws-hook рапортует `pane_alive=False` →
    демот в OFFLINE + disconnect; контроль: `pane_alive=True` → НЕ демот.
- **Реализация:** поле `Peer.busy_since: datetime|None` (Optional, обратносовм.
  в персисте). Новый фоновый цикл `unsafe_sweep_loop(interval=~30с)` в app.py
  lifespan, вызывает существующую `_demote_unsafe_connected_peers`
  (peer_registry.py:1303 — уже пингует pane_alive). Конфиг
  `daemon.unsafe_sweep_interval_sec` (дефолт 30). Опц.: гейтить pane-ping на
  peer'ах с `busy_since` старше `daemon.busy_probe_after_sec` (дефолт 120) —
  busy-длительность = триггер пинга, НЕ критерий эвикции.
  Reaping осиротевших ws-hook: sweep уже демоутит при pane_alive=False;
  дополнительно — сверка `pane_id` с `tmux list-panes` если ws-hook не отвечает.
- **DoD:** busy-зомби после смерти панели уходит OFFLINE за ≤ sweep-интервал
  без ручного kill_peer. Живой долгий turn НЕ убивается. Юнит + интеграционный.

---

## Общие требования (high blast-radius)
- TDD: каждый task — тест-первый, один фикс за раз, прогон полного test-suite
  после каждого.
- Не трогать дешёвый `liveness_tick` (5с) — он остаётся WS-only; pane-ping
  идёт отдельным sweep-циклом (B-3), чтобы не наращивать нагрузку.
- `verification-before-completion` перед PR: прогнать `pytest`/линт, привести
  вывод. Если есть интеграционные тесты против живого tmux/демона — отметить,
  какие прогнаны.
- PR: английский title/body; описать риск рестарта; раздел про то, что merge+
  рестарт-2 — по команде devops-head/director.
- Не мержить. Сообщить devops-head по готовности (PR + зелёный CI).

## Порядок исполнения для worker
1. A-task-1 → A-task-2 → A-task-3 (→ A-task-4 investigation, эскалация если
   system-пробел). Коммит A, прогон тестов.
2. B-1 → B-2 → B-3. Коммит B, прогон тестов.
3. Открыть PR (A+B вместе; если B затягивается — сначала PR только A).
4. `verification-before-completion`, отчёт devops-head с SHA/PR# и итогами
   тестов. НЕ мержить.
