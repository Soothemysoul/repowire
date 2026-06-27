# beads-rbox — Mesh display token economy (design)

**Дата:** 2026-06-27
**Автор:** backend-worker
**Статус:** на checkpoint у backend-head (impl НЕ начат)
**Репо:** repowire-fork (`Soothemysoul/repowire`), ветка `feat/mesh-display-token-economy`

## 1. Задача и scope

Сократить размер **отображаемого** mesh-payload без потери корректности:

1. **DISPLAY** — убрать суффикс `-claude-code` (и любой `-<backend>`) из отображаемого
   peer-name + укоротить отображаемый `[#notif-XXXXXXXX]` в двух сайтах:
   - **(A)** mesh-display = pane-инъекция агентам (agent↔agent);
   - **(B)** telegram-bot display (user-facing).
2. **enforcement/truncation** — **ОТЛОЖЕНО в follow-up** (подтверждаю по брифу head'а).
   rbox даёт только display-механику; три прозовых правила
   `_shared/message-economy.md` НЕ дублируются.

### HARD-инвариант (директор, merge-gate)

Канонический `notif-XXXXXXXX` **неприкосновенен** во всех внутренних путях:
correlation-extraction, ACK-watchdog, `query_tracker`, ack-state, interrupt-ledger.
Меняется **только display-слой**. PR не мержится без теста, доказывающего отсутствие
регрессии correlation/ACK после смены отображаемого префикса.

## 2. Ключевая находка архитектуры

Префикс `[#notif-XXX]` **пишется в одном месте** — `mcp/server.py:345`:
```python
"text": f"[#{correlation_id}] {message}",   # ТОЛЬКО путь notify
```
…и затем **парсится из того же поля `text`** во всех компонентах:

| Компонент | Парсер | Назначение (НЕ ломать) |
|---|---|---|
| `hooks/websocket_hook.py` | `_NOTIF_ID_IN_TEXT_RE`, `_INTENT_ACK_RE`, `_parse_correlation_id` | AUTO-ACK / intent-ACK / receipt-intercept |
| `daemon/routes/messages.py` | `_INTERRUPT_CORRELATION_RE` | interrupt-ledger (`text_prefix`, `correlation_id`) |
| `daemon/peer_registry.py` | hold-queue regex | telemetry held-msg |
| `telegram/bot.py` | `_NOTIF_ID_RE` | reply-map (`_tg_msg_to_notif`) |

**Вывод:** токен в `text` — двойного назначения (display + correlation). Поэтому
правило проектирования: **парсинг correlation работает на сыром wire-`text`, а
укорачивание — это чистое presentation-преобразование, применяемое ПОСЛЕ парсинга,
только в точке отображения.** wire-`text` остаётся каноническим → инвариант директора
выполнен «бесплатно» (interrupt-ledger `text_prefix` тоже сохраняет `[#notif-...]`,
что прямо проверяет существующий `test_interrupt_jsonl_log`).

### Scope токенов по типам сообщений

- `notify` → `text = "[#notif-XXX] msg"` (есть и суффикс-имя, и notif-токен).
- `broadcast` → `text = msg` (notif-токена НЕТ; есть только суффикс-имя в `@{from_peer}`).
- `ask_peer`/query → `text = query`, correlation в отдельном поле (notif-токена в тексте НЕТ).

→ **notif-укорачивание касается только notify**; suffix-strip имени — notify+broadcast (pane) и telegram.

## 3. Предлагаемая архитектура (Option A — display-time transform)

Новый чистый модуль presentation-преобразования (предположительно в
`repowire/naming.py`, рядом со `strip_backend_suffix`, как SSOT
build/strip/display):

```python
def display_peer_name(name: str) -> str:
    """Стрип -<backend> суффикса для отображения. Реюз strip_backend_suffix."""

def display_text(text: str) -> str:
    """Укоротить ведущий [#notif-XXX] -> <короткая форма> для отображения.
       Только presentation; wire-text не меняется."""
```

Применяется **ровно в двух точках**, на сырых `from_peer`/`text`, ПОСЛЕ всей
correlation-логики:

- **(A) pane:** `websocket_hook.py` — `f"@{display_peer_name(from_peer)}: {display_text(text)}"`
  (notify :707) и `f"@{display_peer_name(from_peer)} [broadcast]: {text}"` (:757).
- **(B) telegram:** `bot.py` — отображаемая строка `_tg_send(...)` использует
  `display_peer_name(who)` / `display_text(text)`; **в reply-map `_tg_msg_to_notif`
  хранится сырой `text`/`who`** (correlation сохранён).

Всё, что парсит correlation, получает нетронутый wire-`text` → ноль регрессий по
конструкции.

## 4. Два решения для checkpoint (нужен ack head'а/директора)

### Решение 1 — scope суффикс-стрипа на pane-сайте (A)

**Конфликт:** DoD просит срезать `-claude-code` у agent↔agent имён. Но bare-name
резолюция (`_alias_resolve_unlocked`, beads-7ijt.1) резолвит укороченное имя
**только для `bypasses_circles` пиров** (service/orchestrator/human: telegram,
director, brain-admin, slack, dashboard). Обычные AGENT-namesakes **намеренно НЕ**
stem-aliased — это **Variant A, апрув директора** (минимизация blast radius, bof3).

Эмпирика этой сессии: `notify_peer('backend-head')` → **404 Unknown peer**;
`notify_peer('backend-head-claude-code')` → ok. Т.е. если показать `@backend-head:`,
а LLM скопирует это bare-имя в `notify_peer` — **404** (footgun).

**Варианты:**
- **1a (рекомендую):** на pane-сайте срезать суффикс **только там, где bare резолвится**
  = тот же предикат `bypasses_circles` (telegram/director/brain-admin/slack/dashboard).
  Обычные агенты сохраняют полное имя `@backend-head-claude-code:`. Display ↔
  addressability консистентны; Variant A соблюдён. Экономия концентрируется на
  высокочастотном director/telegram-трафике.
- **1b:** срезать у всех + расширить bare-резолюцию на обычных агентов → **отвергнуто
  директором (Variant A)**; потребует переоткрытия решения у директора.
- **1c:** срезать у всех чисто косметически, без bare-резолюции → footgun (404), отвергаю.

→ **Рекомендация: 1a.** Если нужна полная экономия и на agent↔agent именах — это
эскалация к директору на переоткрытие Variant A (head флагнул вовлечение директора
по раскату, можно склеить).

На **telegram-сайте (B)** суффикс-стрип безопасен для **любого** имени: пользователь
не вызывает `notify_peer`, адресация идёт через sticky-routing/`/select` бота.

### Решение 2 — форма укороченного notif-токена (только notify-pane + telegram)

Риск: на pane LLM-receiver читает префикс и авторит intent-ACK `ACK notif-XXX`
(`_INTENT_ACK_RE` требует префикс `notif-`). Если показать `[XXX]` (голые 8 hex), LLM
может написать `ACK XXX` без `notif-` → intent-ACK не распарсится.

Смягчение: AUTO-ACK эмитится **хуком из wire-`text`** (не LLM) и закрывает watchdog
независимо от intent-ACK. Т.е. watchdog не регрессирует даже при сломанном intent-ACK.

**Варианты:**
- **2a (рекомендую для pane):** `[#notif-XXX]` → `[notif-XXX]` (убрать только `#`).
  Reconstruction-safe (полный `notif-XXX` виден LLM), regex intent-ACK не затронут.
  Экономия скромная (1 символ), но zero-risk по correctness — приоритет директора.
- **2b:** `[#notif-XXX]` → `[XXX]` (убрать `#notif-`, экономия 7 символов). Требует
  явного ack, что intent-ACK reconstruction-risk приемлем (покрыт AUTO-ACK). Возможно
  с параллельным обновлением ACK-шаблона в `_shared/*` (agent-instruction →
  claude-md-improver, вне scope rbox).
- **2c:** на telegram (user-facing, ACK не авторит) можно укорачивать агрессивно (`[XXX]`
  или вовсе убрать notif из показа) — безопасно.

→ **Рекомендация: 2a на pane** (zero-risk), **2c-агрессивно на telegram**. `[XXX]` на
pane — по явному ack head/директора.

## 5. Раскат (НЕ инициируем до ack директора)

- Изменения в `mcp/server.py` нет (wire-формат не трогаем) → **MCP-сервер перезапуска не требует**.
- Изменения только в `hooks/websocket_hook.py` (pane) и `telegram/bot.py`.
  - **websocket_hook** грузится per-session хуком → подхватывается на
    **hook-refresh-on-spawn** (новые сессии), без mesh-disruptive рестарта демона.
  - **telegram/bot.py** — отдельный долгоживущий процесс (`repowire telegram start`),
    требует рестарта бота (локально, НЕ mesh-disruptive для агентов).
- Демон (`daemon/*`) не меняется → **рестарт демона не нужен** (не mesh-disruptive).
- **Финальное окно раската — за директором** (head флагнул mesh-disruptiveness);
  реализация пишется до PR независимо от окна.

## 6. План тестов (TDD, merge-gate)

Новые/изменённые тесты (pytest, конвенции `tests/`):

1. **`tests/test_naming.py`** (unit) — `display_peer_name` стрипает каждый `AgentType`
   суффикс; bare/unknown возвращает как есть. `display_text` укорачивает ведущий
   `[#notif-XXX]` по выбранной форме; текст без префикса не трогает; префикс не в начале
   не трогает.
2. **MERGE-GATE (correlation не регрессировал):** payload с укороченным **display** всё
   равно резолвит canonical `notif-XXX`:
   - `websocket_hook._parse_correlation_id` / `_classify_receipt` на wire-`text`
     `[#notif-XXX] …` → возвращает полный `notif-XXX` (display-преобразование на
     отдельной строке не влияет);
   - **`test_interrupt_jsonl_log` сохранён**: `entry["text_prefix"].startswith("[#notif-...]")`
     и `correlation_id == notif-XXX` (wire-text не тронут);
   - telegram `_NOTIF_ID_RE` извлекает `notif-XXX` из сырого `text`, reply-map хранит сырой.
3. **pane-инъекция (A):** при notify от `backend-head-claude-code` инжектится строка с
   `display_peer_name`/`display_text`; при notify от `director-claude-code` суффикс срезан
   (вариант 1a); ack-state по-прежнему получает полный `notif-XXX`.
4. **telegram display (B):** `_tg_send` получает укороченную строку; `_tg_msg_to_notif`
   хранит полный `notif-XXX` и сырой `from_peer`.

## 7. Out of scope (явно)

- enforcement/truncation длины payload (follow-up);
- дублирование прозовых правил `message-economy.md`;
- изменение wire-протокола / `mcp/server.py` формата;
- расширение bare-резолюции на обычных агентов (если решение 1 → 1b, это отдельная
  эскалация к директору, отдельный bead).
