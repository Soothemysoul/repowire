# Design Spec — mesh-wide restart-awareness (beads-q3v5)

- **Status:** Design approved by director (Approach A, notif-02744ccd, 2026-06-22). Awaiting user-OK before implementation.
- **Date:** 2026-06-22
- **Owner:** backend-head (вёл beads-lfn6 watchdog в repowire-fork)
- **Beads:** parent `beads-q3v5` → subtasks `beads-k1b3` (L2, repowire-fork) + `beads-tbwa` (L1, system)
- **Связь:** `beads-7mxq` (context-restart, источник gap), `beads-lfn6` (liveness-aware watchdog — переиспользуется), nfap (out-of-band receipts / ack-state), `beads-dgjr` (idle-kill, смежный lifecycle)

---

## 1. Проблема

Подчинённый агент (pm / head / worker) при переполнении контекста **self-рестартится** (механизм `beads-7mxq` Phase 2: Stop-хук детектит % контекста → `AGENT_RESTART=1` + resumption brief + autoresume). Это **by-design**, не регрессия.

Недостающий кусок — **взаимосвязанные пиры (в первую очередь супервизор) об этом НЕ знают**. Последствия:

1. **Потеря сообщений в окне рестарта.** Супервизор шлёт `notify_peer` в момент, когда подчинённый между teardown и respawn. Текущее поведение daemon `/notify` (`repowire/daemon/routes/messages.py`):
   - к пиру в статусе `OFFLINE` → reject-error («Peer X is offline»);
   - при silent-death доставки → `503 SERVICE_UNAVAILABLE`.
   - tmux-очередь спасает **только пока pane жив** (busy mid-turn) — а при рестарте pane умирает, буфера нет.
2. **Нет сигнала о context-разрыве.** Супервизор не знает, что надо верифицировать восстановление состояния подчинённого (resume-brief + beads).
3. **Узнаёт косвенно** — через watchdog/эскалацию третьей стороны (как в исходном инциденте: director узнал об offline PM только через 503 при доставке от devops).

**Исходный инцидент (пользователь, 2026-06-22):** zeon PM self-рестартнулся по переполнению, director (супервизор) не был сигнализирован; «планировали что начальники перезапускают подчинённых, а PM самостоятельно перезапустился».

---

## 2. Выбранная модель (approved)

**Подход A — «self-restart остаётся + добавляем awareness»:** self-restart (7mxq) работает и НЕ меняется; добавляется (а) сигнал супервизору и (б) сохранность сообщений. Строится на уже существующем: 7mxq resume-flow, lfn6 status-aware watchdog, nfap ack-state, `PeerStatus`.

> **Подход B (отклонён):** supervisor-driven restart (супервизор сам детектит переполнение и триггерит). Требует доступа супервизора к live-context-% подчинённого (его нет) → большой rebuild телеметрии. Достигает той же awareness существенно дороже.

Решение разбивается на **два слоя**:

| Слой | Цель | Repo | Beads |
|---|---|---|---|
| **L1 — сигнал / observability** | Супервизор знает о рестарте подчинённого | `system` | `beads-tbwa` |
| **L2 — durability** | Нет потери сообщений в окне рестарта + watchdog restart-aware | `repowire-fork` | `beads-k1b3` |

---

## 3. Утверждённые решения (open-decisions)

- **Q1 — hold-queue ДОЛЖНА быть DURABLE.** In-memory отклонён (теряется при daemon-restart = противоречит цели). Предпочтение: **(i) daemon durable spool** (separation-of-concerns: hold-queue ≠ receipt-state, не перегружает nfap-семантику). **(ii) nfap-piggyback** приемлем только если чисто расширяет ack-state без размытия receipt-логики — выбор на этапе impl, durable обязательно.
- **Q2 — ОБА сигнала (pre + post).** Pre («ухожу в рестарт» → супервизор паузит отправку, daemon холдит) критичен для user-требования «знать о рестарте». Post wake-ack («вернулся, continuity ок, claimed») закрывает верификацию непрерывности.
- **Q3 — ОТДЕЛЬНЫЙ restart-cap** в watchdog, НЕ переиспользовать lfn6 busy-cap (у respawn+resume другой таймскейл, дольше busy-turn). Размер такой, чтобы **застрявший рестарт** (не вернулся за N мин) всё равно эскалировал — не маскировать failed-restart навечно.
- **Q4 — два координированных PR** (L2 repowire-fork + L1 system), катить согласованно: L2 (RESTARTING-статус + hold-queue) должен существовать до того, как L1 начнёт его выставлять.

---

## 4. Детальный дизайн

### 4.1 L2 — repowire-fork (beads-k1b3)

**(a) Новый статус `PeerStatus.RESTARTING`**
- `repowire/protocol/peers.py` — добавить `RESTARTING = "restarting"` в enum (рядом с `ONLINE`/`BUSY`/`OFFLINE`).
- `/status` route (`daemon/routes/messages.py:168`, валидация «online, busy, offline») — расширить допустимый набор на `restarting`.
- Registry-переходы (`daemon/peer_registry.py`): легальные переходы `ONLINE/BUSY → RESTARTING` (по сигналу подчинённого) и `RESTARTING → ONLINE` (по respawn / WS-reconnect). При WS-reconnect существующая логика уже ставит `ONLINE` — нужно убедиться, что она триггерит flush очереди (см. (b)).

**(b) Daemon hold-queue для RESTARTING-пира**
- `/notify` (`daemon/routes/messages.py:219`): при `peer.status == RESTARTING` — **НЕ** reject/503, а **enqueue в durable spool** (per-peer, FIFO, с timestamp + correlation_id).
- Хранилище (Q1): **(i)** отдельный daemon spool — напр. `$BRAIN_*/repowire/holdq/<peer_id>.jsonl` (durable на диске, переживает daemon-restart). **(ii)** альтернатива — расширение nfap ack-state, только если не размывает receipt-семантику.
- **Flush:** при переходе `RESTARTING → ONLINE` (respawn) daemon доставляет накопленную очередь в порядке FIFO, затем очищает spool. Доставка — обычным транспортом (tmux inject / WS), с сохранением correlation_id для ack-трекинга.
- **Bound:** ограничить размер/возраст очереди (защита от безграничного роста, если рестарт «застрял» — см. restart-cap (c)); при превышении — эскалация отправителю (как настоящий delivery-fail).

**(c) Watchdog restart-aware (на базе lfn6)**
- `receiver_is_live()` (`repowire/hooks/utils.py:378`, lfn6 liveness-проба через `daemon_get`): `RESTARTING` трактуется как **live/grace** (аналогично `BUSY`) — watchdog НЕ эскалирует «не подтверждён».
- **Отдельный restart-cap** (Q3): новый env, напр. `REPOWIRE_RESTART_GRACE_*` (НЕ переиспользовать `REPOWIRE_ACK_*` busy-cap). Размер ~ типовое время respawn+resume (несколько минут) с запасом. По исчерпании — **эскалация** (рестарт застрял = настоящий fail, не маскировать).

### 4.2 L1 — system (beads-tbwa)

**(a) PRE-restart сигнал** (`ops-typed/cmd/agent-stop/main.go`, путь context-overflow, перед teardown):
- Подчинённый ставит свой статус `RESTARTING` в daemon (через `/status`).
- `notify_peer('<supervisor>', 'context-overflow restart, возобновляюсь из brief, claimed: <beads>')`.
- Порядок load-bearing: статус `RESTARTING` выставляется **до** того, как pane умрёт, чтобы daemon уже холдил входящие.

**(b) POST-restart wake-ack** (`ops-typed/src/ops_typed/hooks/session_start_d/subordinate_resume_brief.py` + `autoresume-trigger`):
- На первом успешном turn после respawn: статус → `ONLINE` (авто через WS-reconnect) → daemon flush'ит hold-queue.
- Wake-ack `notify_peer('<supervisor>', 'возобновлён, continuity ок, claimed: <beads>')` — обобщение существующего director wake-ack-к-пользователю на **подчинённый → супервизор**.
- Определение `<supervisor>` — из роли/circle (как в существующем resume-flow / chain-of-command).

### 4.3 Координация катки (Q4)

1. **PR #1 (repowire-fork, beads-k1b3):** PeerStatus.RESTARTING + /status расширение + daemon hold-queue + watchdog restart-cap. Деплой = daemon-restart.
2. **PR #2 (system, beads-tbwa):** agent-stop pre-signal + resume-flow post wake-ack. Зависит от PR #1 (RESTARTING-статус + hold-queue должны уже жить в daemon).

---

## 5. Gates / инварианты (не сломать)

- nfap out-of-band receipt-путь, AUTO-NACK на реальных сбоях, `REPOWIRE_RECEIPT_INLINE` rollback — нетронуты.
- lfn6 exactly-once two-phase sweep — сохранить; добавление RESTARTING в liveness-ветку не должно ломать busy/offline-логику.
- Различение `RESTARTING` (hold+grace) от `OFFLINE` (реальный fail → reject/escalate) — **не маскировать настоящие delivery-fail** и застрявшие рестарты.
- self-restart модель 7mxq — **не меняется**, только дополняется сигналом.

## 6. DoD (из beads-q3v5)

При self-restart подчинённого по context-overflow: (1) его супервизор получает сигнал (pre + post), знает о рестарте + context-разрыве, может верифицировать resume; (2) сообщения, отправленные в окне рестарта, **не теряются** (hold-queue + flush по respawn), а не дропаются 503; (3) watchdog **не** даёт false-positive на known-restarting пире, но застрявший рестарт (>restart-cap) всё равно эскалирует.

## 7. Вне scope / отложено

- Supervisor-driven restart (Подход B) — отклонён.
- Перестройка телеметрии context-% для супервизора — не требуется в Подходе A.
- idle-kill lifecycle (`beads-dgjr`) — смежный, отдельный.

---

## 8. Open implementation choices (решаются на impl, не блокируют user-OK концепта)

- Q1 финал: (i) daemon spool vs (ii) nfap-piggyback — выбор по чистоте расширения receipt-семантики.
- Точное N для restart-cap (env-подбор по типовому respawn+resume).
- Формат spool-файла + locking (по образцу nfap ack-state flock-дисциплины).
