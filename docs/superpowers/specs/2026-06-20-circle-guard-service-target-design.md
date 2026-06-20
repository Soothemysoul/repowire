# beads-8lzb — Forward-fix: circle-guard должен пропускать unresolved sender к service/global таргетам

**Тип:** P0 bug (регрессия от beads-hqvm DoD7, merge-SHA `8a7f514`)
**Repo:** repowire-fork · **Файл:** `repowire/daemon/peer_registry.py`
**База ветки:** `main` (содержит hqvm) — это форвард-фикс, НЕ revert.

## Симптом (репро в проде)

Пользователь из Telegram пишет director → демон отвечает:
```
Circle boundary: unresolved sender cannot access director-claude-code (global)
```
Канал user→director (критичнейший) отрезан.

## Корень

hqvm DoD7 ужесточил `_check_circle_access_by_peers` (peer_registry.py:976-982):
при `from_obj is None` (unresolved sender) на non-bypass пути теперь
**безусловный** `raise`, вместо прежнего free-pass (early-return-on-None).

telegram-gateway релеит сообщение пользователя в director. Для демона его
sender НЕ резолвится в зарегистрированный peer (`from_obj=None`) и
`bypass_circle` не выставлен → срабатывает DoD7-raise. Это **легитимный**
unresolved sender (gateway/CLI), а не leak-кейс.

Текущий порядок проверок (баг — raise стоит до учёта типа таргета):
```python
if bypass: return
if not to_obj: return
if not from_obj:
    raise ValueError("Circle boundary: unresolved sender cannot access ...")  # ← всегда
if from_obj.bypasses_circles or to_obj.bypasses_circles: return
if from_obj.circle != to_obj.circle: raise ...
```

`bypasses_circles` (peers.py:115) = `role in (SERVICE, ORCHESTRATOR, HUMAN)`.
director=ORCHESTRATOR, telegram/brain-admin=SERVICE → у всех `True`. То есть
именно service/global таргеты, достижимые извне circle-системы по дизайну.

## Фикс (точечный, ~4 строки)

Поднять проверку `to_obj.bypasses_circles` **выше** raise на unresolved sender.
Service/global таргет достижим из-за пределов circle-системы по дизайну (user
через gateway, CLI). Unresolved sender блокируется ТОЛЬКО при project-scoped
таргете (реальный leak-кейс).

```python
def _check_circle_access_by_peers(self, from_obj, to_obj, bypass):
    if bypass:
        return
    if not to_obj:
        return
    if to_obj.bypasses_circles:
        # beads-8lzb: service/global таргет (director/telegram/brain-admin)
        # достижим извне circle-системы по дизайну (user через telegram-gateway,
        # CLI — легитимный unresolved sender). Должно стоять ДО guard'а на
        # unresolved sender, иначе legit gateway→director режется (регрессия hqvm).
        return
    if not from_obj:
        # beads-hqvm DoD7 (сохранён, refine: теперь только для project-scoped
        # таргета): unresolved sender на non-bypass пути к project-peer = leak,
        # DENY.
        raise ValueError(
            f"Circle boundary: unresolved sender cannot access "
            f"{to_obj.display_name} ({to_obj.circle})"
        )
    if from_obj.bypasses_circles:
        return
    if from_obj.circle != to_obj.circle:
        raise ValueError(
            f"Circle boundary: {from_obj.display_name} ({from_obj.circle}) "
            f"cannot access {to_obj.display_name} ({to_obj.circle})"
        )
```

Инвариант сохраняется: для resolved sender логика прежняя
(`from_obj.bypasses_circles or to_obj.bypasses_circles` распадается на две
ветки, обе покрыты). leak-fix (authenticated from_peer_id, scope резолва
таргета по circle отправителя) живёт в `_resolve_pair_unlocked` и НЕ трогается.

## TDD (обязательно — это пропущенный verify из hqvm)

Файл `tests/test_circles.py`. Сначала тесты (красные), потом фикс.

1. **PASS:** unresolved sender (non-bypass) → service/orchestrator таргет
   (director/telegram/brain-admin) проходит. Зарегистрировать таргет с
   `role=PeerRole.ORCHESTRATOR` (и/или SERVICE) в circle `global`; вызвать
   `pm.notify("telegram-gateway", "<target>", "...")` без `bypass_circle` →
   успех, без `ValueError`. Это прямой репро прод-бага.
2. **BLOCKED:** unresolved sender (non-bypass) → project-scoped таргет
   (`role=AGENT`) остаётся заблокирован (`ValueError, match="Circle boundary"`).
   Уже есть `test_unknown_sender_non_bypass_blocked` — добавить явный аналог в
   `TestAuthenticatedSenderResolution`/рядом, чтобы инвариант был виден.
3. **leak-fix цел:** прогон существующего `TestAuthenticatedSenderResolution`
   (double-collision → sender circle, no silent foreign delivery, guard
   hardening) — должен остаться зелёным без изменений.

Проверить, что `test_unknown_sender_non_bypass_blocked` (target role=AGENT)
по-прежнему проходит — он и есть кейс №2.

## DoD

- [ ] Фикс в `_check_circle_access_by_peers` (порядок проверок).
- [ ] 3 группы тестов выше (новые + регрессия зелёные).
- [ ] `pytest tests/test_circles.py` полностью зелёный.
- [ ] PR в repowire-fork от `fix/circle-guard-service-target-unresolved` → base `main`.
- [ ] PR title/body — English; в body упомянуть beads-8lzb, beads-hqvm, репро.

## Redeploy (НЕ входит в задачу worker'а)

Деплой демона откачен на pre-hqvm (devops). После merge devops-head делает
повторный daemon-restart; verify ОБЯЗАТЕЛЬНО включает live telegram→director.
Координирует director/devops-head.
