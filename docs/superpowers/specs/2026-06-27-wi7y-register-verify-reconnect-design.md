# beads-wi7y — register-verify после (re)connect: design-spec

- **Дата:** 2026-06-27
- **Автор:** backend-worker
- **Бид:** beads-wi7y (P2, repo=repowire-fork) · корень beads-s8di · комплементарно beads-jj7l
- **Статус:** DRAFT — на checkpoint у backend-head
- **Gate для:** telegram-display rollout (финальное окно = daemon restart = mass-reconnect)

---

## 1. Проблема

backend-head scope осиротел на 98 минут (s8di): жив, но в mesh-реестре отсутствовал
всю сессию (`PeerLastSeenEpoch==0`), пока zombie-watchdog не снёс его на 60-й минуте.
Корень — потеря mesh-регистрации без какого-либо детекта. wi7y закрывает **детект+retry+alert**;
jj7l закрывает **предотвращение потери** (eviction/grace/auto-reconnect) — это разные половины.

Критично (директор): wi7y ОБЯЗАН покрывать **reconnect**, не только fresh spawn. Финальное окно
раската telegram-display = daemon restart, при котором реконнектятся СУЩЕСТВУЮЩИЕ сессии. Именно
reconnect породил orphan. Без покрытия reconnect wi7y не разблокирует telegram-display.

---

## 2. Root cause (systematic-debugging, Phase 1)

Расследование по реальному коду (не по гипотезам s8di):

### Факт A — `_peers` (источник для watchdog) живёт ТОЛЬКО в памяти
`PeerRegistry._peers: dict[str, Peer]` (`peer_registry.py:145`) — единственный источник
для `GET /peers` (`routes/peers.py:97-109` → `get_all_peers`). Watchdog читает именно
этот endpoint, отсюда берётся `PeerLastSeenEpoch`. **На диск персистится только
`SessionMapping`** (peer_id→identity, `peer_registry.py:147-153`, `sessions.json`) — там
НЕТ liveness/last_seen. Значит при **рестарте демона `_peers` стирается полностью**;
`last_seen`, статус и сам факт «peer сейчас зарегистрирован» исчезают.

### Факт B — регистрация fire-and-forget, без read-back
WS-hook (`websocket_hook.py:910-963`): шлёт `connect`, получает `connected`
(`websocket.py:179-184`) и ДОВЕРЯЕТ ему. Никакой проверки, что peer реально присутствует
в `GET /peers`, нет. В рамках одного живого демона `connected` ⟹ peer в `_peers` (демон
шлёт `connected` строго после `allocate_and_register`+`transport.connect`,
`websocket.py:150-184`) — то есть «получил connected, но не зарегался» внутри одной жизни
демона не бывает.

### Факт C — реальный silent-gap = reconnect НЕ состоялся
После рестарта демона `_peers` пуст; каждая живая сессия обязана реконнектнуться
(`ConnectionClosed → _reconnect_loop → re-register`, `websocket_hook.py:896-986`), чтобы
вновь появиться в `_peers`. Единственное, что реконнектит существующую сессию, —
**это её собственный, уже запущенный WS-hook-процесс**. Если reconnect не происходит
(hook-процесс мёртв / не стартовал; либо коннект надолго недоступен и `connected` ещё не
получен), сессия жива, но в `_peers` её нет → `PeerLastSeenEpoch==0` всю сессию → reaper
сносит orphan на 60-й минуте. **Это дословно s8di.**

### Вывод
Регистрация существующей сессии после рестарта демона ничем активно не верифицируется и не
алертится. `connected`-handshake необходим, но недостаточен: (1) он подтверждает только
WS-handshake, не устойчивую видимость в `GET /peers`; (2) он вообще не приходит, если
reconnect не запущен. Нужен **активный register-verify**: после (re)connect подтвердить,
что peer реально в реестре; если нет → retry re-connect + alert.

---

## 3. Самый важный дизайн-вопрос: hook-version gap

**Проблема:** verify в WS-hook — per-session, hook обновляется только при respawn сессии.
Существующие сессии в окне daemon restart реконнектятся со СТАРЫМ hook'ом и нового verify
НЕ получат. То есть client-side verify сам по себе НЕ покрывает критичное окно.

Варианты (поставлены директором):

### (a) daemon-side verify — детект на демоне, активен для ВСЕХ независимо от версии hook'а
Демон сам обнаруживает peer'ов, которые должны были реконнектнуться, но не сделали этого,
и алертит.

**Препятствие (по коду):** после рестарта `_peers` пуст, а персистентный `SessionMapping`
НЕ хранит liveness — mapping остаётся и для давно умерших сессий (на загрузке отсеивается
лишь по `Path(path).exists()`, `peer_registry.py:192-196` — слабый прокси: worktree живёт
дольше сессии). Значит у демона нет надёжного множества «был жив, должен реконнектнуться».
Чтобы сделать (a) точным, нужна **новая персистентная liveness-state** — а это ровно
территория jj7l (eviction/heartbeat/reconnect-grace). Крудовый (a) без неё = шумные
false-positive алерты на давно мёртвые mappings.

### (b) pre-respawn — респавн сессий до рестарта, чтобы они подняли новый hook
Это **ops-процедура** (director-driven), не код в repowire-fork. Директор уже заявил, что
будет мониторить `list_peers` и респавнить выпавших вручную в окне раската.

### Рекомендация воркера (на adjudication у head)
**Многослойно, без дублирования с jj7l:**

1. **Client-side register-verify (ядро wi7y, в repowire-fork WS-hook)** — после `connected`
   активно проверить присутствие себя в `GET /peers`; при отсутствии в рамках ограниченного
   budget'а → force-reconnect (unbounded loop уже есть) + one-shot alert. Покрывает fresh +
   reconnect для сессий на НОВОМ hook'е. Конкретно, тестируемо, ложится сейчас.

2. **Окно daemon restart (старые hooks) закрывается операционно вариантом (b)** —
   pre-respawn / ручной мониторинг директора (уже запланирован). Client-side verify
   становится эффективным fleet-wide по мере естественного respawn сессий на новый hook.
   Это честное ограничение, которое надо зафиксировать явно.

3. **Робастный daemon-side «expected-reconnect» детектор откладывается в jj7l**, где
   персистентная liveness/eviction-state и так должна жить. Так wi7y (детект+retry+alert,
   client-side) и jj7l (предотвращение потери, daemon-side liveness+grace) не пересекаются.

> ⚠️ Это и есть точка, которую директор пометил как «без решения wi7y есть, но не работает
> в нужный момент». Воркер рекомендует (b)+client-verify для окна и defer daemon-side в jj7l.
> **Финальное решение — за head на checkpoint.** Если head хочет крудовый daemon-side detect
> в составе wi7y (приняв риск шума/частичного дубля jj7l) — это меняет scope, обсудим.

---

## 4. Предлагаемое решение (client-side, ядро wi7y)

Все изменения — в `repowire/hooks/websocket_hook.py`, внутри `_reconnect_loop`. Покрываем
ОБА пути единым кодом: и fresh spawn, и reconnect идут через `_reconnect_loop` (fresh — это
первый проход цикла, reconnect — последующие после `ConnectionClosed`).

### 4.1 Post-connect verify
После получения `connected` и записи метаданных (`websocket_hook.py:932-959`) — добавить шаг:

```
verify: GET /peers/by-pane/{pane_id}  (endpoint существует: routes/peers.py:111)
  ok  = ответ 200 И peer_id из ответа == session_id из handshake
  если ok      → _pane_warn_clear, обнулить verify-fail счётчик, продолжить message loop
  если НЕ ok   → залогировать, инкремент verify-fail счётчика,
                 при verify_fail >= порога → alert (см. 4.3),
                 закрыть сокет и continue (вернуться в начало loop = forced reconnect)
```

Verify ловит редкую гонку «handshake ok, но в реестре не виден» и любую будущую дивергенцию
(defense-in-depth). HTTP-verify изолирован от WS — отдельный сигнал.

### 4.2 Connect-failure alert (уже частично есть)
Сейчас при N неудачных коннектах подсвечивается только tmux-pane (`_pane_warn_set`,
порог `_WARN_AFTER_ATTEMPTS=3`, `websocket_hook.py:984-985`) — это видит человек у панели,
но НЕ супервайзер по mesh. Расширить: при достижении порога — **one-shot** alert супервайзеру/
human (дедуп, чтобы не спамить каждую попытку).

### 4.3 Alert path (reuse, не изобретать)
Переиспользовать существующий `_daemon_post("/notify", ...)` (`websocket_hook.py:336-341,
370-413`) — тем же механизмом, что AUTO-(N)ACK. Текст — RU-summary + canonical IDs
(пер `_shared/peer-communication.md`), помечен как INFRA, target — супервайзер (через
`BRAIN_AGENT_ROLE`-производную цепочку) и/или telegram. One-shot с дедупом по pane-state
(аналогично `_warn_active`), сбрасывается при успешном verify.

Точные детали target-routing alert'а (кому именно: own-head vs director vs telegram) —
**open question на checkpoint** (см. §7).

### 4.4 «Nothing polls» соблюдён
Verify — НЕ новый таймер/loop: он встроен в уже существующий event-driven reconnect-цикл и
срабатывает один раз на каждый (re)connect. Это не нарушает Lazy-Repair философию.

### 4.5 Граница: что wi7y НЕ делает
- НЕ трогает eviction/heartbeat/grace на демоне (это jj7l).
- НЕ добавляет персистентную liveness-state (это jj7l).
- НЕ чинит «hook-процесс мёртв» — это зона `supervise()` (`websocket_hook.py:1002-1026`,
  уже есть) + операционный мониторинг директора. Client-verify по определению не выполняется,
  если процесс, который его выполняет, мёртв.

---

## 5. Разграничение с jj7l (no dup)

| | wi7y (этот спек) | jj7l |
|---|---|---|
| Суть | детект потери регистрации + retry + alert | предотвращение потери |
| Сторона | client (WS-hook) | daemon (eviction/heartbeat) |
| Механизм | post-connect verify в `GET /peers`, alert | reconnect-grace / auto-reconnect / liveness-state |
| Вопрос | «как заметить, что выпал» | «почему выпадает и как не дать выпасть» |

---

## 6. План тестирования (TDD)

Расширяем `tests/hooks/test_websocket_hook_reconnect.py` (head: «test уже есть, расширь»).
Стиль файла — monkeypatch module-globals, asyncio, mock `websockets.connect` через
async-context-manager (см. `_Boom` в существующем файле). Сначала падающие тесты:

1. `test_verify_passes_when_peer_present` — handshake `connected` + `GET /peers/by-pane`
   возвращает наш peer_id → verify ok, входим в message loop, warn cleared.
2. `test_verify_fails_when_peer_absent_forces_reconnect` — `connected`, но verify (GET)
   peer'а не находит → сокет закрывается, цикл уходит на повторный connect (forced reconnect).
3. `test_verify_failure_triggers_alert_after_threshold` — N подряд verify-fail → ровно один
   `_daemon_post("/notify", ...)` (one-shot дедуп).
4. `test_connect_failure_alert_is_one_shot` — N неудачных connect → один alert супервайзеру
   (не каждую попытку).
5. `test_reconnect_path_also_verifies` — второй проход цикла (после `ConnectionClosed`) тоже
   гоняет verify (доказывает покрытие reconnect, не только fresh).
6. `test_verify_success_resets_alert_dedup` — после успешного verify дедуп-флаг сброшен
   (следующий сбой снова заалертит).

Регрессия: весь `pytest` зелёный (текущие 222–231 тестов), `ruff check`, `uv run ty check`.
Hooks ставятся из установленного пакета — после правок `uv tool install --force --reinstall .`
перед запуском hook-зависимых тестов.

---

## 7. Open questions для checkpoint

1. **Главное:** утверждаем рекомендацию §3 ((b)+client-verify, daemon-side detect → jj7l)?
   Или head хочет крудовый daemon-side detect внутри wi7y (scope ↑, риск дубля jj7l)?
2. **Alert routing:** кому шлёт alert client-verify — own-head? director? telegram? Воркер
   (`_shared/user-facing-comms.md`) не user-facing → по идее own-head/supervisor через mesh.
   Но hook-процесс — инфраструктура, не агент; у него нет chain-of-command. Нужен канонический
   target для INFRA-alert (вероятно тот же путь, что no_peer_record.go alert в s8di-PR #340).
3. **Verify timing:** verify сразу после `connected`, или с маленькой задержкой/одним retry
   (на случай микрогонки персиста)? Предлагаю: один немедленный verify; при fail — не
   ждать, а сразу forced-reconnect (быстрее закрывает gap).
4. **Дедуп-гранулярность:** one-shot на pane-state (как `_warn_active`) или с TTL-повтором
   (повторный alert через X мин, если всё ещё не зарегистрирован)?

---

## 8. Риски

- **session-lifecycle / reconnect — осторожно** (директор пометил отдельным ревью). Verify
  не должен ломать happy-path: при недоступности `GET /peers` (демон мигает) verify не должен
  ронять соединение зря — трактовать сетевую ошибку verify как «не подтверждено, но мягко»
  (лог + следующий проход), а жёсткий forced-reconnect — только при явном «peer отсутствует».
- **Alert-шум.** Дедуп обязателен; без него mass-reconnect окна зальёт супервайзера.
- **Hook-version gap (§3)** — фундаментальное ограничение client-side, закрывается операционно.
