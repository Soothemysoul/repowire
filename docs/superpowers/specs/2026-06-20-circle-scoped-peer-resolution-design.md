# Spec: circle-scoped peer resolution (fix cross-circle leak)

- **beads:** beads-hqvm (родитель beads-3nkj), P1, project=agents-brain-team, repo=repowire-fork
- **Дата:** 2026-06-20
- **Автор:** backend-worker (agents-brain-team)
- **Статус:** черновик → ожидает sign-off директора
- **Ветка:** `fix/circle-scoped-peer-resolution`

---

## 1. Проблема

Два (и более) peer'а делят один `display_name` в разных circles (например
`backend-head-claude-code` в `project-agents-brain-team` и в `project-zeon`).
`notify_peer('<имя>')` / `ask_peer('<имя>')` БЕЗ явного `circle=` резолвит target
по preference-tiebreak (`connected` / `pane_id` / `last_seen`), **игнорируя circle
отправителя**. Это вызывает два класса дефектов:

1. **Поломка same-circle резолюции** (исходно P2): сообщение к одноимённому
   peer'у в своём circle может уйти к namesake чужого circle и быть отклонённым
   circle-guard'ом (`Circle boundary`) — доставки нет.
2. **ПОДТВЕРЖДЁННАЯ cross-circle УТЕЧКА** (эскалация → P1): когда коллизируют
   **оба** имени (и `to_peer`, и `from_peer`), guard обходится и сообщение
   **молча доставляется в чужой circle**.

### 1.1. Эмпирическое подтверждение (не на веру)

Repro на уровне `PeerRegistry` (см. `/tmp/repro_leak.py`, воспроизведено
2026-06-20): зарегистрированы `backend-head` и `backend-worker` в circle `abt` и
`zeon`; preference смещён так, что abt-namesake выигрывает tiebreak. Реальный
отправитель — zeon-`backend-worker`, вызывает `notify(from_peer="backend-worker",
to_peer="backend-head")` без circle. Результат:

```
Delivered to session: sid-abt-head
LEAK CONFIRMED: routed to WRONG circle (abt); real sender is zeon
```

Сообщение ушло в **чужой** circle (abt), хотя отправитель — zeon. Это совпадает с
живыми датапоинтами: (#1) intent-ACK zeon-worker'а долетел до abt-head; (#2)
**блокирующий** decision-request zeon-worker'а ушёл в abt-head, worker
застопорился, разблокировано relay'ем директора.

### 1.2. Механизм утечки (трассировка кода)

`peer_registry.notify()` / `query()` (circle=None, bypass=False):

1. `peer = _lookup_peer_unlocked(to_peer, circle=None)` (L998 / L916) —
   preference-tiebreak выбирает **abt-head** (чужой circle).
2. `_resolve_from_peer_unlocked(from_peer, target=abt-head, bypass)` (L867):
   `_lookup_peer_unlocked("backend-worker", circle="abt")` находит
   **abt-worker** (namesake!) → `from_obj = abt-worker` (НЕ настоящий zeon).
3. `_check_circle_access_by_peers(abt-worker, abt-head, bypass=False)` (L880):
   `abt == abt` → SAME circle → **проходит** → доставка в abt-head. **УТЕЧКА.**

Корень: демон выводит circle отправителя из **неоднозначного name-lookup**
(scoped к уже-мис-резолвленному target'у), а не из аутентифицированной
идентичности. Когда коллизируют оба имени, name-lookup подтверждает ложный
self-consistent (но неверный) выбор.

### 1.3. Дополнительный дефект — early-return-on-None в guard

`_check_circle_access_by_peers` (L886): `if not from_obj or not to_obj: return`.
При неразрешённом `from_obj` (non-bypass) guard **молча пропускает** вместо отказа.
Это вторая дыра: нерезолвленный отправитель получает свободный проход.

---

## 2. Затронутые узлы (по графу repowire-fork + чтению кода)

| Узел | Файл:строка | Роль |
|---|---|---|
| `_lookup_peer_unlocked` | `repowire/daemon/peer_registry.py:255` | name/peer_id lookup + preference |
| `_resolve_from_peer_unlocked` | `peer_registry.py:867` | резолв отправителя (старая эвристика) |
| `_check_circle_access_by_peers` | `peer_registry.py:880` | circle-guard |
| `query` / `notify` / `broadcast` | `peer_registry.py:900 / 983 / 1024` | точки входа маршрутизации |
| `QueryRequest`/`NotifyRequest`/`BroadcastRequest`, `/query`,`/notify` | `daemon/routes/messages.py` | HTTP-слой; CLI auto-bypass L160-161 |
| `send_query` / `send_notification` | `daemon/message_router.py:28 / 96` | WS-payload получателю |
| MCP `notify_peer`/`ask_peer`/`broadcast`, `_get_my_peer_name` | `mcp/server.py` | sender-side identity |
| AUTO-ACK `_emit_auto_ack`/`_emit_auto_nack`, `handle_message` | `hooks/websocket_hook.py:204/226/407` | reverse-route ACK |
| `/response`, `resolve_query` | `routes/messages.py:302`, `query_tracker.py:143` | response-канал (уже по pane_id→peer_id, безопасен) |

---

## 3. Корневой фикс — аутентифицированная идентичность отправителя

**Ключевая идея:** circle отправителя выводить из **аутентифицированной
идентичности** (`peer_id`), а не из неоднозначного name-lookup.

### 3.0. Важная поправка к формулировке брифа (на sign-off)

Бриф говорит «реальный peer_id/session из websocket_transport, через который пришло
сообщение». Фактически `notify`/`query`/`broadcast` приходят по **HTTP POST**
(MCP → daemon), а не по WS; per-call WS-идентичности на этом пути нет. Сильнейший
доступный аутентифицированный якорь — **pane→peer_id**, который MCP-сервер уже
получает через `/peers/by-pane/{pane_id}` в `_get_my_peer_name`, но **выбрасывает**,
оставляя только коллизирующий `display_name`. Решение: MCP пробрасывает свой
`peer_id` (`from_peer_id`) в запрос; демон резолвит отправителя по нему.

Это устраняет **случайную** мис-маршрутизацию (реальный баг). От подделки
`from_peer_id` это не защищает — но текущая name-схема ровно так же подделываема,
а модель угроз — кооперирующие агенты, не злоумышленник. Anti-spoof — вне scope.

### 3.1. Part A — sender side (`mcp/server.py`)

- `_get_my_peer_name` дополнительно кэширует `_cached_peer_id` из pane-lookup.
- `notify_peer`/`ask_peer`/`broadcast` добавляют `from_peer_id` в тело запроса,
  когда он известен. Поле **опционально** (back-compat).

### 3.2. Part B — daemon resolution (`peer_registry.py` + `routes/messages.py`)

- Routes: `QueryRequest`/`NotifyRequest`/`BroadcastRequest` получают опциональное
  `from_peer_id: str | None`. `query`/`notify`/`broadcast` в registry — тоже.
- **Гибрид (нулевой регресс для legacy):**
  - **Authenticated-путь** (есть `from_peer_id`, нет явного `circle`, non-bypass):
    1. `from_obj = _lookup_peer_unlocked(from_peer_id)` (peer_id — ключ словаря →
       точный резолв).
    2. Если `from_obj` и не `bypasses_circles`: target =
       `_lookup_peer_unlocked(to_peer, circle=from_obj.circle)` (scoped к circle
       отправителя). Если scoped дал None → fallback на unscoped lookup (guard
       поймает cross-circle и бросит `Circle boundary` — явная ошибка, не молчание).
    3. `_check_circle_access_by_peers(from_obj, peer, bypass)`.
  - **Legacy-путь** (нет `from_peer_id`, либо явный `circle`, либо bypass):
    **существующее поведение без изменений** (`_lookup(to_peer, circle=circle)` →
    `_resolve_from_peer_unlocked`). Все текущие тесты остаются зелёными.
- **Приоритет явного `circle=`** сохраняется (circle≠None → выигрывает всегда).
- **bypass / `bypasses_circles`** (director/service/orchestrator): scoping НЕ
  применяется — они легитимно ходят в чужие circles.

### 3.3. Part C — guard hardening (DoD 7)

`_check_circle_access_by_peers`: при `bypass=False` и `from_obj is None` →
**RAISE** `Circle boundary` (отказ), а не early-return. `to_obj` на путях
`query`/`notify` всегда не-None (раньше бросается `Unknown peer`).

⚠️ **Изменение семантики (на sign-off):** ломает текущий тест
`test_unknown_peer_no_enforcement` (`pm.query("cli", ...)` non-bypass, from None).
Реальный CLI на route ставит `bypass=True` (L161), runtime-путей с
None+non-bypass нет. Предлагаю обновить тест на новую политику (unknown sender +
non-bypass = blocked). **Требует подтверждения директора.**

### 3.4. Part D — AUTO-ACK reverse-route (DoD 6)

Сейчас AUTO-ACK (`hooks/websocket_hook.py`) шлёт `/notify` обратно по
**имени** исходного отправителя с `bypass_circle=True` (bypass отключает и
scoping) → reverse-route ACK мис-роутится при коллизии.

- `send_notification` / `send_query` (`message_router.py`): добавить
  `from_peer_id` в WS-payload получателю.
- `handle_message` (`websocket_hook.py`): читать `from_peer_id = data.get(...)`,
  пробрасывать в `_maybe_emit_receipt` → `_emit_auto_ack`/`_emit_auto_nack`.
- AUTO-ACK/NACK: POST `/notify` с `to_peer_id=<from_peer_id>` (точный id), а не
  по имени. `/notify` route + `notify()` принимают опциональный `to_peer_id`;
  при наличии — `_lookup_peer_unlocked(to_peer_id)` (без неоднозначности).

### 3.5. Part E — broadcast sender-resolution

`broadcast()` (L1043) резолвит `from_peer` по имени (неоднозначно) перед
circle-фильтром excludes. Добавить резолв по `from_peer_id`, когда передан.

### 3.6. Транзитивный фикс intent-ACK

Intent-ACK (LLM зовёт `notify_peer('<исходный отправитель>', 'ACK ...')`)
чинится **транзитивно**: forward-фикс (Part B) доставляет исходное сообщение
правильному получателю → его LLM отвечает в пределах **своего** (верного)
circle → intent-ACK авто-скоупится назад корректно. Отдельный код не нужен;
покрывается тестом.

### 3.7. response/decision-канал (датапоинт #2)

`/response` + `query_tracker.resolve_query` маршрутизируют по
`correlation_id` + `pane_id`→`peer_id` — **уже безопасно** (не по имени).
decision-request (датапоинт #2) шёл как `notify`/`query` — закрывается Part B.
Кода не требует; подтверждается тестом.

---

## 4. Развёртывание

`repowire` — единый PyPI-пакет (`uv tool install`); daemon и MCP обновляются
**атомарно** одной установкой. Окна рассинхрона daemon↔MCP нет. На время
rollout legacy-путь (старый MCP без `from_peer_id`) сохраняет прежнее поведение
(включая остаточный риск утечки) — но т.к. пакет единый, после установки оба
конца новые.

---

## 5. План тестов (TDD, до фикса — красные)

DoD 1-8 + регрессы. Файл: `tests/test_circles.py` (+ при необходимости
`tests/` для ws-hook AUTO-ACK).

| # | Тест | DoD |
|---|---|---|
| T1 | both-names-collide + `from_peer_id` zeon → доставка в zeon (не abt) | 1,2,7,8 |
| T2 | both-names-collide, реальный отправитель → НЕ молчаливая доставка в чужой circle | 2,8 |
| T3 | `from_obj=None` + non-bypass → RAISE (guard hardening) | 7 |
| T4 | director/service bypass cross-circle по-прежнему работает | 3 |
| T5 | явный `circle=` имеет приоритет над авто-скоупом | 4 |
| T6 | AUTO-ACK reverse-route по `to_peer_id` → точный исходный отправитель | 6 |
| T7 | intent-ACK транзитивно корректен (forward к верному получателю) | 6 |
| T8 | pm↔pm коллизия в обоих circles — регресс | 8 |
| T9 | не сломаны: CLI auto-bypass, broadcast circle-фильтр, get_peer/kill_peer | 5 |
| T10 | legacy-путь (без from_peer_id) = прежнее поведение (существующие тесты) | 5 |

---

## 6. Открытые вопросы для sign-off директора

1. **Guard hardening семантика (§3.3):** ОК обновить `test_unknown_peer_no_enforcement`
   на «unknown sender + non-bypass = blocked»? (Реальный CLI использует bypass —
   runtime не затрагивается.)
2. **Поправка к механизму брифа (§3.0):** аутентификация через pane→peer_id (MCP),
   а не через WS-сессию (notify/query — HTTP). Подтвердить, что это приемлемый
   «authenticated identity».
3. **Scope Part C/D/E:** фикс AUTO-ACK reverse-route расширяет правку на
   `message_router` + `websocket_hook` (DoD 6 этого требует). Подтвердить охват.
4. **Upstream-PR:** делать ли отдельный PR в `Soothemysoul/repowire` помимо
   fork-патча? (на усмотрение — бриф допускает.)

### Решения sign-off директора (notif-c6b2c26c, 2026-06-20)

1. Guard hardening (unknown sender + non-bypass = blocked, DoD 7) — **одобрено**,
   обновить `test_unknown_peer_no_enforcement`.
2. Поправка механизма pane→peer_id (`from_peer_id` вместо WS-сессии) —
   **благословлена** как корректный «authenticated identity»; основной фикс.
3. Scope Part C/D/E (AUTO-ACK reverse-route → `message_router` + `websocket_hook`) —
   **одобрено**, покрыть T6/T7.
4. Upstream-PR — **НЕ в этом PR.** Этот PR = только fork-патч. Upstream-citizenship
   вынесен в follow-up **beads-4wuz** (после мержа fork-патча).

---

## 7. Риски

- **Чувствительная mesh-инфра:** баг ломает все коммуникации → тщательный TDD,
  обязательный полный прогон `pytest` + `ruff` + `ty`.
- **Гибрид legacy/authenticated** минимизирует регресс, но удваивает пути —
  покрыть оба тестами (T10).
- **Concurrency:** вся резолюция под `self._lock` — новые helpers вызываются
  внутри уже взятого lock (unlocked-варианты).
