# Design: фикс circle-clobber + hardening reaper (beads-q2ok)

- **Дата:** 2026-06-15
- **Эпик:** beads-q2ok (P1, project=agents-brain-team)
- **Subtask'и:** beads-lyfk (A, circle-clobber, P1), beads-nxxm (B, reaper, P2)
- **Репозиторий:** repowire-fork (демон)
- **Автор дизайна:** devops-head
- **Статус:** на ревью у director (checkpoint перед merge+рестартом)

> Blast radius: правки в mesh-демоне, от которого зависят ВСЕ живые агенты.
> Рестарт демона рвёт коннекты. Merge + рестарт — только после greenlight
> director'а и координации окна с drafter-pm (#96→#95+wupw).

---

## Контекст (из RCA)

Два независимых дефекта, всплывшие в одном инциденте (недоступность director
через Telegram).

### Симптом A — circle-clobber
`global-view-<project>` — это НЕ circle, а tmux-сессии, слинкованные с `global`
(`tmux new-session -d -s global-view-<tag> -t global`, source:
`system/ops-typed/src/ops_typed/brain_workspace.py:181-187`). Linked/grouped
tmux-сессии **шарят те же окна и панели**, включая панель director'а.

repowire ставит **глобальный** (`-g`) tmux-hook `after-rename-session`
(`repowire/hooks/tmux_lifecycle.py:54-60`), который на любой rename вызывает
`tmux_rename_hook.sh` с `list-panes -s` (session-scoped, без `-t`) —
`repowire/hooks/tmux_rename_hook.sh:15,17`. Хендлер
`handle_session_renamed` **слепо** переписывает circle каждого peer'а из списка
панелей в имя новой сессии: `set_peer_circle(peer, new_name)`
(`repowire/daemon/lifecycle_handler.py:90-95`).

Итог: при rename, затрагивающем linked-сессию `global-view-*` (которая шарит
панель director'а), хендлер уводит director (и brain-admin/librarian — они тоже
в `global`) из circle `global` в `global-view-agents-brain-team`. Telegram-бот
роутит сообщения director строго scoped `circle="global"`
(`repowire/daemon/app.py:148`, `repowire/telegram/bot.py:93,158,529`), lookup
фильтрует строго `p.circle == circle`
(`repowire/daemon/peer_registry.py:255-271`) → в `global` остаётся мёртвая
offline-запись → юзер недоступен. Рестарт поднимает director обратно в `global`
корректно, но следующий rename снова клоббит.

**Подтверждение вживую (list_peers, 2026-06-15):** в реестре до сих пор висят
зомби-артефакты — `repow-global-view-agents-brain-team-e450f512`
(director, circle=global-view-agents-brain-team, offline) и аналогичный
librarian.

### Симптом B — busy-зомби (reaper)
Reaper существует: `liveness_tick_loop` каждые 5с
(`repowire/daemon/app.py:108-111`, `peer_registry.py:1371-1429`), НО сверяет
статус ТОЛЬКО с WS-коннектом (`transport.is_connected`), не с pane-liveness и
не с длительностью busy.

`busy` ставится UserPromptSubmit-хуком (`prompt_handler.py:24-29` →
`websocket.py:244-251`), снимается ТОЛЬКО Stop-хуком (`stop_handler.py:71-79`).
Панель умерла mid-turn → Stop не сработал → busy не снят. ws-hook — **detached**
процесс (`session_handler.py:265`, `start_new_session=True`), переживает смерть
панели и держит WS открытым → `is_connected=True` → `liveness_tick` считает
peer'а «здоровым» → busy навсегда. Pane умирает — обнаруживается лениво только
при `kill_peer` (`spawn.py:171-188,300-305` → `Tmux pane not found`).

**Ключевое:** функция `_demote_unsafe_connected_peers`
(`peer_registry.py:1303-1336`) УЖЕ делает ровно то, что нужно — пингует ws-hook,
читает `pong.pane_alive` и демотит peer'а с мёртвой панелью. Но вызывается
только из `lazy_repair` (`peer_registry.py:1263-1279`) — HTTP-триггер,
debounce 30с. На простаивающем демоне в окне инцидента она не сработала.

---

## Fix A — circle-clobber (beads-lyfk, P1)

### Рассмотренные подходы

**A1 (рекомендую) — guard в хендлере: не трактовать view-сессии как circle.**
В `handle_session_renamed` пропускать circle-rewrite, если `new_name` —
не-circle «view»-сессия. Дискриминатор — **имя** (паттерн), вынесенный в конфиг
(`daemon.tmux.non_circle_session_patterns`, дефолт `["global-view-*"]` или
шире `["*-view-*"]`).

*Почему имя, а не «is grouped»:* при `new-session -t global -s global-view-X`
в группу попадают ОБЕ сессии — и base `global`, и view `global-view-X`. Значит
факт членства в группе НЕ различает base от view; единственный надёжный
дискриминатор — naming convention view-сессий. Базовые circle (`global`,
`project-*`) под паттерн не попадают.

- Плюсы: точно бьёт корень; не ломает Tilix UI (view-сессии продолжают жить как
  tmux-сессии, просто никогда не угоняют circle); минимальный diff (одна
  проверка + конфиг-поле); обратимо.
- Минусы: вводит в демон знание о соглашении именования view-сессий (смягчено
  тем, что это конфиг-поле, не хардкод).

**A2 (defense-in-depth, вторично) — сузить сбор панелей в hook через `-t`.**
В `after-rename-session` передавать `list-panes -t <renamed-session>` вместо
session-scoped `-s` без таргета.
- Плюсы: убирает зависимость от «текущего» контекста hook'а.
- Минусы: **сам по себе НЕ чинит** — grouped-сессии шарят окна, поэтому
  `list-panes -t global-view-X` всё равно вернёт общие панели (включая
  director'а). Полезно только как дополнительная страховка поверх A1.

**A3 (отклонён) — резолвить «домашнюю» сессию панели в хендлере.**
Для каждой панели определять её сессию через `display-message` и сравнивать с
`new_name`. Отклонён: для grouped-сессий `#{session_name}` панели неоднозначен —
ненадёжно.

### Выбранный дизайн A
**A1 как основной** + **A2 как дешёвая страховка** (defense-in-depth, по
`systematic-debugging/defense-in-depth.md`):

1. Конфиг-поле `daemon.tmux.non_circle_session_patterns: list[str]`
   (дефолт `["global-view-*"]`) в `repowire/config/models.py`.
2. В `handle_session_renamed`: если `new_name` матчит паттерн (fnmatch) —
   ранний выход с `logger.info`, без `set_peer_circle`. (Сам `LifecycleHandler`
   получает доступ к паттернам через `peer_registry._config` или явный
   конструкторный параметр — выбор на этапе плана, предпочтительно явный
   параметр для чистоты тестирования.)
3. (A2) В `tmux_lifecycle.py` для `after-rename-session` добавить таргет
   `-t #{session_name}` в `list-panes` (правка `_HOOKS` + `tmux_rename_hook.sh`,
   обратносовместимо: `session_name` всё ещё прокидывается).

### DoD A
- Rename/создание linked-сессии `global-view-*` больше НЕ перекидывает
  director/brain-admin/librarian из circle `global`.
- Юнит-тест на `handle_session_renamed`: peer в circle `global`, прилетает
  rename с `new_name="global-view-agents-brain-team"` и панелью этого peer'а →
  circle НЕ меняется; контрольный кейс: легитимный rename базовой сессии →
  circle меняется как раньше.
- Tilix two-pane UI не сломан (linked-сессии создаются/работают как прежде).

---

## Fix B — hardening reaper (beads-nxxm, P2)

### Рассмотренные подходы

**B1 (рекомендую) — периодический pane-liveness sweep, переиспользуя
`_demote_unsafe_connected_peers`.**
Добавить фоновый цикл (отдельный, медленнее `liveness_tick`), который вызывает
уже существующую и протестированную `_demote_unsafe_connected_peers` на cadence
~30с. Она пингует ws-hook, читает `pane_alive`, демотит мёртвых.
- Плюсы: закрывает корень (периодическая сверка с реальной панелью) минимальным
  кодом; переиспользует протестированную функцию; cadence ограничивает нагрузку.
- Минусы: до 30с задержки обнаружения зомби (приемлемо vs текущие ∞).

**B2 — встроить pane-ping в `liveness_tick` каждые 5с.**
- Плюсы: быстрее (≤5с).
- Минусы: пинг ВСЕХ ONLINE/BUSY peer'ов каждые 5с — заметная нагрузка на
  больших mesh; смешивает дешёвую WS-сверку с дорогой pane-сверкой.

**B3 — `busy_since` + busy-timeout как триггер пинга.**
Добавить поле `busy_since` в `Peer` (ставится при переходе в BUSY); в цикле
пинговать pane-liveness ТОЛЬКО для peer'ов, висящих busy дольше
`busy_timeout`. Демотить лишь при реально мёртвой панели.
- Плюсы: самый таргетный (пинги только подозрительных); `busy_since` — полезная
  телеметрия; **никогда не убивает легитимно-долгий, но живой turn** (пинг
  подтверждает жизнь панели).
- Минусы: новое поле в модели + миграция персиста маппингов.

### Выбранный дизайн B
**B1 как основа** + **`busy_since` из B3 как дешёвое наблюдаемое улучшение**:

1. Поле `Peer.busy_since: datetime | None` (`protocol/peers.py`), ставится в
   `_set_peer_status` при переходе в BUSY, сбрасывается при выходе из BUSY.
   Обратносовместимо в персисте (Optional, дефолт None).
2. Новый фоновый цикл `unsafe_sweep_loop(interval_sec=30.0)` рядом с
   `liveness_tick_loop`, запускается в `app.py` lifespan; вызывает
   `_demote_unsafe_connected_peers`.
3. (Опционально, по решению на этапе плана) гейтить дорогой pane-ping в sweep
   на peer'ах с `busy_since` старше `daemon.busy_probe_after_sec` (дефолт ~120с),
   чтобы не пинговать только что начавшиеся turn'ы. ONLINE-peer'ы пингуются по
   обычной cadence.
4. Конфиг: `daemon.unsafe_sweep_interval_sec` (дефолт 30),
   `daemon.busy_probe_after_sec` (дефолт 120).

> Важно: busy-длительность — это **триггер проверки**, а НЕ критерий эвикции.
> Демотим только при подтверждённо мёртвой панели (`pane_alive=False`). Это
> исключает убийство медленного, но живого turn'а.

### DoD B
- Busy-зомби после смерти панели автоматически уходит в OFFLINE за
  ≤ `unsafe_sweep_interval_sec` без ручного `kill_peer`.
- Юнит/интеграционный тест: peer BUSY + ws-hook рапортует `pane_alive=False` →
  периодический sweep демотит в OFFLINE + disconnect; контроль: живой busy-peer
  (`pane_alive=True`) НЕ демотится.
- Нет регресса нагрузки на дешёвом `liveness_tick` (он не трогается).

---

## План отгрузки и рестарта

- **A приоритетнее B** (A — фикс аутэйджа). Дефолт — A+B одним
  протестированным изменением (ветка `fix/q2ok-circle-reaper`) + ОДИН
  координированный рестарт демона. Если дизайн/реализация B затянется —
  отгрузить A (+ twwz) первым restart-окном, B — вторым.
- **twwz (gsd-dev 403)** едет в том же restart-окне (config-строка
  `spawn-claude gsd-dev` в `~/.repowire/config.yaml` + сверка формата
  prefix-allowlist в `spawn.py`), без отдельного brainstorm. Отдельный слой,
  общего корня с A/B нет.
- **Рестарт-окно** (CRITICAL): только ПОСЛЕ (1) greenlight director'а по этому
  дизайну, (2) drafter-pm домержил #96→#95 и завершил wupw, (3) director
  предупредил юзера. Рестарт оборвёт коннекты, включая сессию самого director'а.
- **Реализация** делегируется devops-worker (ветка/worktree
  `fix/q2ok-circle-reaper`); PR держится ОТКРЫТЫМ до greenlight; merge+рестарт —
  по команде director'а.

## Открытые вопросы к director (checkpoint)
1. A: достаточно ли `non_circle_session_patterns=["global-view-*"]`, или сразу
   шире `["*-view-*"]` на случай новых view-семейств?
2. B: cadence sweep 30с приемлема, или нужно быстрее (с учётом нагрузки)?
3. Запускать ли реализацию через devops-worker СЕЙЧАС (PR откроется, merge
   подождёт greenlight), или ждать твоего ОК по дизайну перед спавном воркера?
