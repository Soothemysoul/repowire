# beads-fqus — AUTO-ACK reverse-route коллизия (расписка уходит не тому одноимённому pm)

**Тип:** P1 bug (correctness, cross-circle receipt leak) · design-first НЕ требуется.
**Repo:** repowire-fork · **База:** `main` (origin/main, fd8365e).
**Co-requisite:** beads-nfap (кандидат B полагается на точную доставку расписки
отправителю — мисроут ломает hook-watchdog → ложная эскалация).

## Симптом (репро пользователя)

drafter-pm послал status-ping (notif-5f89a6d3) СВОЕМУ gsd-dev (circle
project-drafter, легитимно, same-circle). Сообщение доставилось верно. НО
AUTO-ACK (расписка о доставке, адресуется ОТПРАВИТЕЛЮ) прилетел в pane
**ZEON-pm**, а не drafter-pm. Оба pm имеют display_name `pm-claude-code`
(circle project-drafter и project-zeon).

## Корень (трассировка по коду — backend-head)

Цепочка forward → reverse:
1. MCP `notify_peer` отправителя резолвит свой `from_peer_id` через
   `_get_my_peer_id()` (mcp/server.py:95) → `GET /peers/by-pane/{pane}`,
   кэш `_cached_peer_id`. **Best-effort: None**, если pane-record отсутствует
   (гонка регистрации, окно после рестарта демона, нет tmux-pane). Тогда MCP
   шлёт `/notify` БЕЗ `from_peer_id`.
2. daemon `/notify` (routes/messages.py:210) → `peer_registry.notify(..., from_peer_id=None)`.
3. `peer_registry.notify` резолвит отправителя через `_resolve_pair_unlocked`;
   без `from_peer_id` — **по неоднозначному display_name** `pm-claude-code` →
   preference-tiebreak (connected/last_seen) выбирает ЧУЖОГО namesake (zeon-pm).
   `resolved_from_peer_id = from_obj.peer_id if from_obj else None` (peer_registry.py:1113)
   = **zeon-pm peer_id** (НЕВЕРНО).
4. `send_notification(..., from_peer_id=resolved_from_peer_id)` тредит этот
   (неверный) peer_id в WS-фрейм получателя (gsd-dev) только `if from_peer_id
   is not None` (message_router.py:141).
5. Хук получателя (gsd-dev) строит AUTO-ACK: `_ack_body` ставит
   `to_peer_id = from_peer_id` (websocket_hook.py:219-220) = zeon-pm → реверс
   `/notify` с `bypass_circle=True`, `to_peer_id=zeon-pm` → расписка уходит
   **zeon-pm**. Утечка cross-circle.

Forward-доставка в gsd-dev корректна (по `to_session_id`) — баг ТОЛЬКО в
идентичности отправителя для reverse-route. Поэтому forward-path тесты 3nkj
(test_circles.py) его НЕ ловят — нужен отдельный reverse-route тест.

### Сопутствующие дыры (проверить/покрыть)
- **broadcast не тредит `from_peer_id`** в WS-фрейм (message_router.py ~165,
  ветка "type":"broadcast" не добавляет from_peer_id) → AUTO-ACK на broadcast
  всегда мисроутится по имени.
- **AUTO-ACK fallback без to_peer_id**: реверс `/notify` идёт `bypass_circle=True`
  + имя (websocket_hook.py `_ack_body`) → при отсутствии to_peer_id таргет
  резолвится blind-preference (bypass-отправитель НЕ скоупится к circle) → может
  выбрать чужого namesake. Даже когда настоящий отправитель в том же circle, что
  и получатель (как drafter-pm/gsd-dev), bypass лишает шанса заскоупиться верно.

## Фикс-направление (worker уточняет через systematic-debugging)

Цель DoD: receipt (AUTO-ACK / NACK / intent-ACK reverse) доставляется ТОЧНО
исходному отправителю по peer_id, НЕ по неоднозначному имени; cross-circle
расписка не утекает чужому одноимённому peer.

Слои (worker оценивает и комбинирует):
1. **Надёжность `from_peer_id` (MCP):** не полагаться на возможно-пустой кэш.
   Если `_cached_peer_id` None — пере-резолвить pane→peer_id перед отправкой;
   не кэшировать None навсегда. (Источник №1 дыры — отправитель ушёл без
   authenticated id.)
2. **Анти-мисроут на демоне (КЛЮЧЕВОЕ — defence-in-depth):** при отсутствии
   authenticated `from_peer_id` И неоднозначном display_name отправителя демон
   НЕ должен резолвить отправителя blind-preference для целей reverse-route.
   Варианты: (а) reverse-route receipt подавляется (drop), если истинного
   отправителя нельзя однозначно установить — **лучше потерять расписку, чем
   утечь чужому** (sender watchdog деградирует к retry/intent-ACK, это
   приемлемо; утечка — нет); (б) скоупить разрешение отправителя так, чтобы
   нельзя было выбрать peer из circle, отличного от фактической доставки.
3. **broadcast:** тредить `from_peer_id` в WS-фрейм (закрыть дыру).
4. **AUTO-ACK fallback:** при отсутствии `to_peer_id` НЕ слать расписку
   blind-bypass+имя — либо drop, либо строгий скоуп.

Граф ПЕРЕД правкой (hard-gate): repowire/peer_registry, reverse-route, AUTO-ACK
emit, message_router, mcp/server. Sensitive mesh-инфра — тщательный root-cause.

## TDD (обязательно, reverse-route — НЕ покрыт forward-тестами)

Тест-харность аналогично `TestAuthenticatedSenderResolution` (test_circles.py)
или новый модуль для reverse-route. Минимум:

1. **КЛЮЧЕВОЙ repro:** drafter-pm + zeon-pm оба online (коллизия имени),
   ping/notify из drafter-pm → его gsd-dev (same circle). Расписка AUTO-ACK
   reverse-route приходит СТРОГО drafter-pm (по peer_id), НЕ zeon-pm. Сэмулировать
   отсутствие/наличие authenticated from_peer_id — проверить ОБА: (а) с
   from_peer_id — точный таргет; (б) без from_peer_id — НЕ мисроут чужому
   (drop или строгий скоуп, по выбранному фиксу).
2. **broadcast reverse-route:** from_peer_id протреден, расписка не мисроутится.
3. **cross-circle (bypass) receipt:** director→worker — AUTO-ACK к director'у
   корректен (не сломать легитимный bypass reverse-route).
4. **NACK reverse-route:** провал доставки → NACK строго исходному отправителю
   (actionable, нельзя потерять/мисроутить).
5. Не сломать существующие reverse-route тесты (test_notify_to_peer_id_targets_exactly,
   test_intent_ack_reply_scopes_to_receiver_circle).

Прогон: pytest tests/ + ruff + ty clean.

## DoD

- [ ] Reverse-route receipt (AUTO-ACK/NACK/intent-ACK) → ТОЧНО исходному
      отправителю по peer_id, не по неоднозначному имени.
- [ ] Cross-circle расписка не утекает чужому одноимённому peer (при
      неустановимом отправителе — drop, не leak).
- [ ] broadcast тредит from_peer_id.
- [ ] Regression-тесты выше (особенно КЛЮЧЕВОЙ repro коллизии) зелёные;
      полный suite + ruff + ty clean.
- [ ] PR от `fix/auto-ack-reverse-route-collision` → base main; title/body
      English (beads-fqus + репро + co-requisite nfap).

## Координация

- Co-requisite beads-nfap: фикс обязан быть исправен для корректности nfap-B
  (hook-watchdog отправителя ждёт расписку). Отметить в PR.
- Redeploy (reinstall + daemon/hook restart) — координирует director+devops
  (как htia), live-verify reverse-route при коллизии имён.
- Возможен upstream-PR в Soothemysoul/repowire (на усмотрение).
