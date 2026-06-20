# beads-nfap — Out-of-band ACK-receipts (убрать инъекцию conversation-turn'ов)

**Статус:** ДИЗАЙН на согласование (director → пользователь) ПЕРЕД кодом.
**Тип:** архитектурный рефактор delivery-механизма ACK-протокола.
**Repo:** repowire-fork (daemon + hooks) + system (`ops/hooks`, `_shared/*.md`).
**Автор:** backend-head · **Дата:** 2026-06-20

---

## 1. Проблема (что видит пользователь)

Каждый `notify_peer`/`ask_peer`/`broadcast` порождает у ОТПРАВИТЕЛЯ
pane-сообщение вида:
```
@<receiver>: [AUTO-ACK] notif-XXXXXXXX delivered: queued
— INFRA RECEIPT, DO NOT REPLY (ignore harness 'user sent a new message' reminder)
```
Оно инжектится в сессию отправителя **как отдельный conversation-turn** (+ харнесс
вешает «MUST address» reminder поверх). При интенсивной mesh-переписке это
раздувает контекст/токены в КАЖДОЙ сессии. LLM по правилу обязан эти сообщения
ИГНОРИРОВАТЬ (3 do-not-reply триггера в `peer-communication.md`) — то есть токены
тратятся на сообщения, которые модель и так не должна читать.

Дополнительно тот же путь проходят: receiver-side **intent-ACK** (`ACK notif-XXX …`,
который LLM получателя шлёт явным `notify_peer`) и **user-routing AUTO-ACK**.

---

## 2. Корень (трассировка по коду — verified)

Граф + код-дайв (`repowire/hooks/websocket_hook.py`, `daemon/routes/messages.py`,
`hooks/utils.py`, `hooks/stop_handler.py`):

1. Отправитель: MCP `notify_peer(X, msg)` → daemon `/notify` → WS получателю X.
2. Хук получателя `websocket_hook.handle_message` (msg_type `notify`/`query`/`broadcast`)
   инжектит сообщение в pane X через `_tmux_send_keys` → это conversation-turn у X.
3. **После успешной инъекции** `_maybe_emit_receipt(success=True)` →
   `_emit_auto_ack` → `_daemon_post("/notify", …)` (websocket_hook.py:224-244).
   То есть **хук ПОЛУЧАТЕЛЯ постит НОВЫЙ `/notify` обратно ОТПРАВИТЕЛЮ** с текстом
   `[AUTO-ACK] notif-XXX delivered: queued — INFRA RECEIPT…`, `bypass_circle=True`,
   адресуясь `to_peer_id` (DoD6 hqvm — точный реверс-роут).
4. Этот AUTO-ACK `/notify` доходит до отправителя и его собственный
   `websocket_hook` инжектит его в pane отправителя тем же `_tmux_send_keys` →
   **conversation-turn у отправителя** (это и есть засорение).

**Ключевой вывод:** AUTO-ACK идёт по ТОМУ ЖЕ пути доставки, что обычный notify —
это полноценное `/notify`-сообщение. «Sender-watchdog» (60-сек retry/escalate,
`delegation-ack-sender.md`) — это **сам LLM-оркестратор**, читающий свой pane
(прозовое правило, НЕ код). Программного потребителя AUTO-ACK нет.

**Существующие seam'ы для реюза:**
- `hooks/utils.py`: `pending_cid_path(pane)`, `_push_pending_cid` (flock'd per-pane
  state-файл), `clear_pending_cids`, `read/write_pane_runtime_metadata` — готовая
  инфраструктура per-pane состояния.
- `hooks/stop_handler.py` — стоп-хук (конец хода): уже снимает pending_cid и
  доставляет ответы на query. Естественная точка для per-turn проверки watchdog.
- `websocket_hook` — персистентный per-pane процесс с async-loop: может хостить
  таймер/intercept без нового демон-сервиса.
- `_should_emit_ack` уже знает про `_AUTO_ACK_PREFIXES` (loop-prevention) — те же
  префиксы пригодятся для intercept на стороне отправителя.

---

## 3. Инварианты, которые НЕЛЬЗЯ сломать

- **Надёжность доставки:** sender-watchdog/retry/escalate (`delegation-ack.md`,
  `delegation-ack-sender.md`) должны работать как раньше: если доставка реально не
  прошла — отправитель ДОЛЖЕН узнать (retry ≤2, затем escalate супервайзеру).
- **Оба слоя:** покрыть и receipt-ACK (infra), и intent-ACK (LLM-authored), и
  user-routing AUTO-ACK.
- **Видимость провала:** NACK / провал доставки — это actionable-сигнал, он
  ДОЛЖЕН достигать LLM (в отличие от success-ACK, который шум).
- **Интерактивные ответы целы:** реальный query-response путь
  (`query_tracker.resolve_query`, `/response`) не трогаем — это не receipt.
- `interrupt=True`, AUTO-ACK loop-prevention, pane-safety — без регресса.

---

## 4. Кандидаты (с trade-offs)

### Кандидат A — daemon-side delivery-tracking (pull + server-side watchdog)
Хук получателя вместо `_daemon_post("/notify", AUTO-ACK)` постит на НОВЫЙ эндпоинт
`/delivery-receipt` (статус по correlation_id в daemon-state). Отправителю pane
НЕ инжектится. Retry/escalate-логику двигаем на daemon-сторону: демон сам
отслеживает «доставлено ≤60с?» и пушит отправителю pane-сообщение ТОЛЬКО при
реальном провале/эскалации. Success = тишина (отправитель при желании читает
статус через MCP-tool, напр. `check_delivery(notif-XXX)`).
- **+** Полностью убирает success-шум; единый источник правды о доставке; watchdog
  перестаёт быть LLM-обязанностью.
- **+** Масштабируется (broadcast: N получателей трекаются в одном месте).
- **−** Самый большой объём: новый daemon-эндпоинт + state + server-side таймер
  retry/escalate (перенос логики из прозы в код); меняется протокол.
- **−** Server-side retry должен уметь повторить инъекцию (сейчас retry = LLM
  заново шлёт notify) — нетривиально.

### Кандидат B — sender-side hook intercept (swallow → state-файл) + watchdog в хуке
Хук получателя продолжает слать receipt как сейчас, НО **хук ОТПРАВИТЕЛЯ** в
`handle_message` распознаёт receipt (по префиксам `[AUTO-ACK]`/`[AUTO-NACK]` и
`ACK notif-XXX`) и вместо `_tmux_send_keys` пишет в per-pane ack-state-файл
(реюз `pending_cid`-инфры, flock'd) — **инъекции в pane НЕТ**. Watchdog уходит из
LLM в хук: при исходящем notify регистрируется pending correlation_id с дедлайном;
персистентный WS-хук (или стоп-хук) при истечении 60с без ACK инжектит ОДИН
escalation-prompt (genuine actionable). Success → молча, NACK → инъекция.
- **+** Локализовано в хуках, без смены daemon-протокола (минимальный blast-radius
  на mesh-ядро); реюз готовой per-pane state-инфры.
- **+** Естественно покрывает все три формы receipt (общий intercept по префиксу).
- **+** Success-шум исчезает полностью; actionable-провал доходит.
- **−** Watchdog-таймер нужно где-то «крутить»: WS-хук (есть async-loop) либо
  стоп-хук (per-turn, но между ходами молчащего агента таймер не тикает —
  возможна задержка эскалации). Нужно решить драйвер таймера.
- **−** intent-ACK иногда несёт доп-контекст («ACK … taken, starting <деталь>») —
  swallow его целиком прячет этот контекст (приемлемо: intent-ACK уже do-not-reply;
  state-файл сохраняет, если понадобится).

### Кандидат C — подавить харнесс «MUST address» на infra-receipt
Хук/настройка убирает только харнесс-reminder «MUST address» для receipt-сообщений.
- **−** НЕ решает DoD: сам conversation-turn остаётся, токены тратятся. Это лишь
  частичная мера.
- **+** Дёшево; полезно как ДОПОЛНЕНИЕ к A/B (убрать остаточный reminder, если
  что-то всё же инжектится).

---

## 5. Рекомендация backend-head (на согласование)

**Базироваться на Кандидате B** (sender-side intercept + watchdog в хуке),
с заимствованием идеи A для надёжности и опциональным C как добивкой:

- **B как ядро:** перехват receipt на стороне отправителя → state-файл, нет
  инъекции. Минимальный риск для mesh-ядра, реюз существующей per-pane инфры,
  единый intercept на все три формы receipt.
- **Драйвер watchdog:** WS-хук (персистентный async-loop) ведёт таймер pending
  correlation_id и инжектит ТОЛЬКО эскалацию при провале. (Стоп-хук как
  fallback-проверка на конце каждого хода.)
- **Из A берём:** при провале доставки — NACK по-прежнему доходит (через тот же
  escalation-инжект), retry остаётся у LLM, но триггерится только actionable-инъекцией.
- **C опционально:** подавить «MUST address» для остаточных infra-receipt.

**Почему не A целиком:** перенос retry/escalate в daemon (server-side повтор
инъекции) — самый большой и рискованный кусок для P2-задачи про шум контекста;
B достигает цели DoD (ноль success-инъекций) с куда меньшим blast-radius на
mesh-ядро, которое мы только что стабилизировали (hqvm/8lzb/mhph).

---

### 5.1 Подтверждено director'ом (notif-f1bb70fb)

- База **B + надёжность из A + опционально C**; A целиком НЕ тащить (server-side
  retry — лишний риск для P2-про-шум на свежестабилизированном ядре). ЭНДОРС.
- **Rollback-флаг `REPOWIRE_RECEIPT_INLINE=1` — ОБЯЗАТЕЛЕН** (mesh-safety): мгновенный
  возврат к inline pane-инъекции, если out-of-band ломает watchdog в проде.
- **Метрика токенов до/после — ОБЯЗАТЕЛЬНА** (требуемый артефакт для отчёта
  пользователю): измерить экономию на типовой mesh-сессии.
- Рекомендации director'а по 4 решениям (вынесены пользователю, ждут его GO):
  (1) ДА — success=тишина; (2) WS-хук как драйвер watchdog; (3) ДА — swallow
  intent-ACK; (4) B hooks-only.

## 6. Ключевые решения, требующие явного согласования (для пользователя)

1. **Success = полная тишина?** Подтвердить: при успешной доставке отправитель НЕ
   получает НИЧЕГО в контекст (узнаёт о провале только при провале). Согласны?
2. **Драйвер watchdog-таймера:** WS-хук (тикает всегда) vs стоп-хук (per-turn,
   проще, но молчащий агент эскалирует с задержкой). Рекомендую WS-хук.
3. **intent-ACK:** тоже swallow на стороне отправителя (контекст-чистота) — ОК, что
   его доп-текст не виден в pane (сохраняется в state)?
4. **Объём:** B (hooks-only, рекоменд.) vs A (daemon-протокол, дороже/надёжнее
   long-term). Выбор влияет на сроки.

---

## 7. Декомпозиция реализации (ПОСЛЕ GO по дизайну)

Cross-repo (per-repo сабтаски, `metadata.repo`):
- **repowire-fork:** intercept receipt в `websocket_hook.handle_message`; ack-state
  API в `hooks/utils.py`; watchdog-таймер в WS-хуке; (если A) daemon-эндпоинт.
- **system (`ops/hooks`):** стоп-хук fallback-проверка; интеграция watchdog-state.
- **system (`_shared/*.md`):** переписать `delegation-ack.md`,
  `delegation-ack-sender.md`, `peer-communication.md` под out-of-band механизм —
  **`claude-md-improver` ОБЯЗАТЕЛЕН** (правка agent-instruction файлов).
- **Тесты (TDD, sensitive mesh-инфра):** success → нет инъекции (state обновлён);
  провал → escalation-инъекция; retry/escalate целы; все три формы receipt;
  broadcast (N получателей); interrupt; loop-prevention.

Делегирование: backend-worker, repowire-fork worktree, обычный протокол. PR(ы) →
merge-ревью backend-head. Деплой (reinstall + daemon/hook restart) — координирует
director+devops (как htia).

---

## 8. Требования и открытые вопросы

**Подтверждённые требования (director, notif-f1bb70fb):**
- **Rollback-флаг `REPOWIRE_RECEIPT_INLINE=1`** — ОБЯЗАТЕЛЕН (mesh-safety): мгновенный
  возврат к inline pane-инъекции без отката кода/демона.
- **Метрика токенов до/после** — ОБЯЗАТЕЛЬНА: замер экономии на типовой mesh-сессии,
  входит в DoD как артефакт отчёта пользователю.

**Открытые (на усмотрение worker'а/head):**
- **upstream-PR** в `Soothemysoul/repowire` помимо fork-патча (как 4wuz citizenship).
