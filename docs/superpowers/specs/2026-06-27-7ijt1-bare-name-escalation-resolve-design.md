# beads-7ijt.1 — надёжный резолв bare-имён `telegram`/`director` для эскалаций секретаря

- **Статус:** DESIGN — на ревью backend-head (правка daemon-резолюции отложена до approve)
- **Дата:** 2026-06-27
- **Автор:** backend-worker
- **Repo:** repowire-fork
- **Parent:** beads-7ijt (persist offline user-facing service peers)
- **Тип:** bugfix (design-first, daemon resolution — риск как pkz8)

---

## 1. Проблема (DoD задачи)

Секретарь (bypasses_circles-пир: `director` orchestrator / `brain-admin` service)
при эскалации зовёт `notify_peer('telegram')` / `notify_peer('director')` —
**документированную special-peer форму адресации** (docstring MCP `notify_peer`:
«Special peers: 'telegram' sends to user's phone»; global CLAUDE.md:
`notify_peer('telegram', msg)`). В leak-инциденте 1/4 такой notify вернул **404
"Unknown peer"**, т.е. эскалация молча не дошла.

DoD: (1) notify из service/global-circle резолвит `telegram`/`director` без 404 +
регресс-тест; (2) НЕ регрессить bof3 — одноимённый namesake в РАЗНЫХ circle всё
ещё даёт ERROR Ambiguous.

---

## 2. Root cause (воспроизведён детерминированно, read-only)

### 2.1 Механизм

Live-пиры регистрируются через `allocate_and_register → _build_display_name`
(`peer_registry.py:828`), который **всегда** строит display_name как
`{folder}-{backend}` (комментарий: «daemon owns the name»). Поэтому в живом
registry:

| Адресуют (bare) | Реальный display_name | role / circle |
|---|---|---|
| `telegram` | `telegram-claude-code` | service / global |
| `director` | `director-claude-code` | orchestrator / global |
| `backend-head` | `backend-head-claude-code` | agent / project-… |

Вся публичная резолюция (`_lookup_peer_unlocked:307`, `_resolve_unique_unlocked:348`)
матчит цель **только точным `display_name == identifier`**. Bare-алиаса /
role-stem-fallback **нет нигде**. Bare `telegram` → 0 совпадений → `None` →
route отдаёт `404 "Unknown peer: telegram"`.

### 2.2 Живое воспроизведение (read-only, `GET /peers/{id}` на работающем daemon)

```
telegram                  -> 404  {"detail":"Peer not found: telegram"}
telegram-claude-code      -> 200  (resolves)
director                  -> 404  {"detail":"Peer not found: director"}
director-claude-code      -> 200  (resolves)
backend-head              -> 404
backend-head-claude-code  -> 200
```

Тот же промах подтверждён живьём: мой первый ACK `notify_peer('backend-head')`
вернул `Daemon error 404: Unknown peer: backend-head`.

### 2.3 ⚠️ Гипотеза head про bof3 — ОПРОВЕРГНУТА

NON-OBVIOUS-указание в брифе: «свежий bof3-фикс (#43, AmbiguousPeerError) —
вероятный корень 404». **Это не так.** bof3 `AmbiguousPeerError` срабатывает
только когда identifier матчит **>1 circle**; здесь bare-имя матчит **0 пиров** и
возвращает `None` ДО любой ambiguity-логики (`_resolve_unique_unlocked:351-352`:
`if not matches: return None`). bof3 дал бы **409**, а инцидент — **404**.
Резолюция `_lookup_peer_unlocked` (точное `==`) bof3 не трогал. Корень —
**bare-имя vs backend-суффиксный display_name**, не circle-scoping.

Это не латентная регрессия конкретно от bof3 — bare-резолва не было и до него;
проявилось, потому что эскалационный путь секретаря использует bare-форму, которую
обещают и docstring MCP, и CLAUDE.md.

---

## 3. Варианты фикса

Цель: при промахе точного `==` — добивать резолв до пира, чей display_name = это же
имя плюс backend-суффикс, **не ломая bof3** и **не трогая регистрацию** (риск).

### Вариант A — узкий stem-alias только для bypasses_circles-пиров ✅ РЕКОМЕНДУЮ

Fallback **только когда точный `==` дал 0 совпадений** (поэтому поведение всех
существующих exact-name-вызовов не меняется). Среди пиров ищем тех, чей
display_name со снятым trailing `-{backend.value}` равен identifier И у которых
`bypasses_circles` (SERVICE / ORCHESTRATOR / HUMAN). То есть алиасим **ровно
эскалационные таргеты** (`telegram`/`director`/`brain-admin`/`slack`/`dashboard`).

- **Точка врезки — один чокпоинт `_resolve_unique_unlocked`.** Эскалация
  секретаря (bypasses_circles-отправитель) в `_resolve_target_unlocked:1031-1034`
  идёт именно через него с `raise_ambiguous=cross_capable=True`; route-precheck
  `get_peer(...)` — туда же. Помощник `_alias_resolve_unlocked(identifier, circle)`
  вызывается из `_resolve_unique_unlocked` (и опционально из `_lookup_peer_unlocked`
  ради `GET /peers/{id}` и внутренних — на усмотрение ревью; минимально достаточно
  только `_resolve_unique_unlocked`).
- **Сохранение bof3:** stem-alias-кандидаты тоже проходят ambiguity-проверку — если
  stem матчит >1 circle и caller cross-capable, поднимаем `AmbiguousPeerError`
  (переиспользуем существующий путь), а не молчаливый pick. На практике
  telegram/director — global-синглтоны, так что неоднозначности нет; защита — на
  будущее.
- **Blast radius:** минимальный. Только bypasses_circles-таргеты; обычные
  агенты по bare role-stem НЕ алиасятся (их и адресуют полным именем или
  `circle.name`-формой).

**Эскиз:**
```python
def _strip_backend(name: str) -> str | None:
    for bk in AgentType:                      # пробуем все backend-суффиксы
        suf = f"-{bk.value}"
        if name.endswith(suf):
            return name[: -len(suf)]
    return None

def _alias_resolve_unlocked(self, identifier, circle=None, *, raise_ambiguous=False):
    # вызывается ТОЛЬКО когда точный == уже промахнулся
    cands = [
        p for p in self._peers.values()
        if p.bypasses_circles and self._strip_backend(p.display_name) == identifier
    ]
    if circle:
        cands = [p for p in cands if p.circle == circle]
    if not cands:
        return None
    circles = sorted({p.circle for p in cands})
    if raise_ambiguous and len(circles) > 1:
        raise AmbiguousPeerError(identifier, circles)
    return self._lookup_peer_unlocked(cands[0].display_name, circle=circle)
```

### Вариант B — общий stem-alias для любого пира

Как A, но без фильтра `bypasses_circles` — алиасить bare role-stem любого агента.
Шире (резолвит и `backend-head`→`backend-head-claude-code`), но и blast radius
больше: bare `pm` начнёт матчить `pm-claude-code` в нескольких проектах →
постоянные `AmbiguousPeerError`. Это меняет контракт адресации агентов в обход
устоявшейся `circle.name`-формы. Вне DoD задачи (она про эскалации секретаря) →
**отклоняю как primary**, оставляю как явно более рискованную опцию.

### Вариант C — регистрировать сервисы под bare display_name (без суффикса)

Снимать суффикс для bypasses_circles-ролей в `_build_display_name`. **Отклоняю:**
трогает регистрацию (identity-reuse, role-sibling-purge, `/peers`-view, mapping-
persist) — самый рискованный путь, ровно класс pkz8. Резолюция-fallback (A)
изолированнее.

---

## 4. План реализации (после approve, TDD)

1. **RED:** `tests/daemon/test_bare_name_escalation_resolve.py`:
   - регистрируем service `telegram-claude-code` (global) + orchestrator
     `director-claude-code` (global) через `allocate_and_register` (как в проде,
     с суффиксом) — не bare, иначе тест мимо бага;
   - bypasses_circles-отправитель `notify('telegram')` / `notify('director')` без
     `circle=` → **резолвится** (сейчас RED: 404);
   - `GET /peers/telegram` → 200 (если алиасим и lookup-путь).
2. **GREEN:** `_strip_backend` + `_alias_resolve_unlocked`, врезка в
   `_resolve_unique_unlocked` (fallback после `if not matches`).
3. **Регресс bof3 (DoD-2):** тест — два `pm-claude-code` в РАЗНЫХ circle, bare
   `notify('pm')` cross-capable → всё ещё `AmbiguousPeerError`/409 (Вариант A их
   не алиасит, т.к. pm не bypasses_circles → поведение bof3 нетронуто; явный тест
   фиксирует инвариант). Прогон `tests/daemon/test_peer_addressing_ambiguity.py`
   зелёным.
4. **Полный suite** (`pytest`), `ruff check`, `uv run ty check`.
5. PR против `origin`, `pr-ci-wait`, **не мержу — ревью head**.

## 5. Открытые вопросы к ревьюеру (head)

1. **Scope алиаса:** Вариант A (только bypasses_circles, узко по DoD) — ок? Или
   нужен B (любой агент по stem)?
2. **Точки врезки:** достаточно `_resolve_unique_unlocked` (notify/query/route-
   precheck), или добавить и в `_lookup_peer_unlocked` (тогда `GET /peers/{id}` и
   внутренние liveness тоже резолвят bare)? Внутренний best-effort останется
   `raise_ambiguous=False`.
3. **`_strip_backend` перебором всех `AgentType`** — приемлемо, или завести явный
   helper в `naming.py` рядом с `build_base_display_name`?
```
