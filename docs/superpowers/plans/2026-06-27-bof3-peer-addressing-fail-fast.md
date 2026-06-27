# beads-bof3 — peer-addressing ambiguity: fail-fast on cross-circle namesake

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:test-driven-development`
> per task (RED → GREEN → REFACTOR). Checkbox (`- [ ]`).

**Goal (P1, USER-flagged 'жёсткий баг системы'):** одинаковый `display_name` в
разных circles (pm в project-drafter И project-agents-brain-team) → `notify_peer`/
`ask_peer`/`kill_peer` БЕЗ circle резолвится в ОДНОГО МОЛЧА (preference-tiebreak)
→ mis-delivery. Продемонстрировано: director (bypasses_circles) release-ACK для
agents-brain-team-pm ушёл drafter-pm. Prose-дисциплина (указывать circle) НЕ ловит
— нужен programmatic fail-fast.

**КОРЕНЬ:** `peer_registry.py:_lookup_peer_unlocked` (L294-302) — при >1 peer с одним
display_name и без circle берёт `max(preference)` (connected/pane/last_seen)
МОЛЧА. Достигается через `_resolve_target_unlocked` unscoped-fallback (L952) и
`get_peer()`.

**Authoritative design — director GO(A) ГИБРИД (notif-8f8e668b):**
- **Слой 1 (ОСТАВИТЬ):** sender-circle auto-disambig для project-scoped sender
  (hqvm leak-fix, `_resolve_target_unlocked` L948) — в своём circle namesake ОДИН,
  это корректность. НЕ трогать.
- **Слой 2 (ДОБАВИТЬ fail-fast):** когда резолв ПОСЛЕ Слоя-1 всё ещё matches >1
  circle И нет `circle`/`to_peer_id` → raise `AmbiguousPeerError` в ПУБЛИЧНОМ пути
  (notify/query/kill/spawn). Молчаливый preference-pick убрать из публичного пути,
  но `_lookup_peer_unlocked` best-effort ОСТАВИТЬ для ВНУТРЕННИХ liveness/repair
  (они не должны падать).
- **НЕТ auto-pick эвристике для bypass-sender** (director/orchestrator/service): у
  глобального orchestrator нет надёжного sender-context, любая эвристика = молчаливый
  mis-delivery в другой форме. fail-fast (заставить указать circle=) = безопасно.

**Tech Stack:** Python (repowire-fork, pytest). repo: repowire-fork (НЕ system).

---

## Conventions
- `AmbiguousPeerError(ValueError)` — subclass ValueError, чтобы существующий route
  error-handling (ValueError → error-response) ловил без изменений. Проверь, как
  routes/messages.py + routes/spawn.py мапят ValueError (вероятно → 4xx/error JSON).
- Fail-fast срабатывает ТОЛЬКО на cross-circle ambiguity (matches охватывают >1
  РАЗНЫЙ circle). Same-circle дубль display_name (аномалия реестра) НЕ триггерит —
  там best-effort preference остаётся.
- PR title/body English. Отметить upstream-citizenship (см. Task 5).

## File Structure
- Modify: `repowire/daemon/peer_registry.py` — `AmbiguousPeerError` + strict-резолв
  helper + wiring в `_resolve_target_unlocked` + `get_peer(raise_ambiguous=...)`.
- Modify: `repowire/daemon/routes/spawn.py` — kill route: strict get_peer.
- (Проверить) `repowire/daemon/routes/messages.py` — notify/query precheck get_peer:
  strict, чтобы ранний actionable error (не двойной резолв).
- Test: `tests/test_circles.py` или новый `tests/daemon/test_peer_addressing_ambiguity.py`.

---

### Task 1: AmbiguousPeerError + actionable message (АКЦЕНТ 1)
- [ ] RED: тест — конструируется ошибка, `str(e)` ==
      `"ambiguous peer 'pm-claude-code': matches [project-agents-brain-team, project-drafter], specify circle="`
      (circles ОТСОРТИРОВАНЫ, перечислены — caller сразу знает что указать).
- [ ] GREEN: класс `AmbiguousPeerError(ValueError)` в peer_registry.py:
      ```python
      class AmbiguousPeerError(ValueError):
          def __init__(self, name: str, circles: list[str]) -> None:
              self.name = name; self.circles = circles
              super().__init__(
                  f"ambiguous peer '{name}': matches "
                  f"[{', '.join(circles)}], specify circle=")
      ```

### Task 2: strict-резолв helper (Слой 2)
- [ ] RED: тесты на helper — 0 match→None; 1 match→peer; >1 в РАЗНЫХ circles +
      raise_ambiguous→AmbiguousPeerError(sorted circles); >1 но raise_ambiguous=False
      → best-effort preference (как сейчас); circle задан → scoped, не raise.
- [ ] GREEN: добавить (НЕ ломая `_lookup_peer_unlocked`, он для internal):
      ```python
      def _resolve_unique_unlocked(self, identifier, circle=None, *, raise_ambiguous=False):
          if identifier in self._peers: return self._peers[identifier]
          matches = [p for p in self._peers.values() if p.display_name == identifier]
          if circle:
              matches = [p for p in matches if p.circle == circle]
          if not matches: return None
          if len(matches) == 1: return matches[0]
          circles = sorted({p.circle for p in matches})
          if raise_ambiguous and len(circles) > 1:
              raise AmbiguousPeerError(identifier, circles)
          return self._lookup_peer_unlocked(identifier, circle=circle)  # best-effort tiebreak
      ```

### Task 3: wiring в публичный путь (notify/query/kill), Слой-1 нетронут
- [ ] `_resolve_target_unlocked`: финальный unscoped-fallback
      `return self._lookup_peer_unlocked(to_peer, circle=None)` (L952) заменить:
      cross-circle-capable sender → strict. Условие:
      ```python
      cross_capable = bypass_circle or from_obj is None or (from_obj is not None and from_obj.bypasses_circles)
      return self._resolve_unique_unlocked(to_peer, circle=None, raise_ambiguous=cross_capable)
      ```
      ОБОСНОВАНИЕ inline: project-scoped sender (не cross_capable) → boundary-guard
      `_check_circle_access_by_peers` сам DENY кросс-circle (Слой-1 семантика цела);
      bypass/orchestrator/legacy → fail-fast на cross-circle ambiguity (инцидент).
      Слой-1 (scoped lookup в свой circle, L948-951) ВЫШЕ — не трогать.
- [ ] kill route (routes/spawn.py ~L289): `get_peer(identifier, circle=request.circle)`
      → передать `raise_ambiguous=True` (kill без circle на ambiguous = опасно, должен
      явно указать). Добавить параметр `raise_ambiguous: bool = False` в `get_peer`
      (дефолт сохраняет всех существующих callers) → прокинуть в `_resolve_unique_unlocked`.
- [ ] notify/query precheck (routes/messages.py get_peer для status, ~L182/L228):
      передать `raise_ambiguous=True` — ранний actionable error вместо молчаливого
      pick + последующего рассинхрона. (Authoritative fail-fast уже в notify()/query()
      через _resolve_target, но precheck не должен врать про status чужого namesake.)
- [ ] spawn route: get_peer всегда с circle (default "default") → scoped → не
      cross-circle ambiguous. НЕ менять (подтвердить тестом-замечанием).

### Task 4: регресс-тесты (АКЦЕНТ 2 — ТОЧНЫЙ инцидент director→pm + АКЦЕНТ 3)
- [ ] Сетап: два pm (`pm-claude-code` @ project-drafter И @ project-agents-brain-team,
      оба ONLINE/видимы) + director (`director-claude-code`, bypasses_circles=True).
- [ ] **ТОЧНЫЙ инцидент:** director.notify(to_peer='pm-claude-code', circle=None,
      bypass/ bypasses_circles) → raise AmbiguousPeerError, message перечисляет ОБА
      circle, НЕ молчаливый preference-pick. (Регресс на peer_registry L294-302.)
- [ ] То же для query() и kill route.
- [ ] director.notify('pm-claude-code', circle='project-agents-brain-team') →
      резолвит ПРАВИЛЬНОГО pm, НЕ raise.
- [ ] **АКЦЕНТ 3 — bypasses_circles-target цел:** notify(to_peer='director-claude-code'
      или 'telegram'/'brain-admin', circle=None) — single global match → доставка
      работает, НЕ raise (не ambiguous).
- [ ] **Слой-1 цел:** project-scoped sender → notify('pm-claude-code') БЕЗ circle, но
      в ЕГО circle pm один → резолвит свой, НЕ raise (sender-circle auto-disambig).
- [ ] internal liveness/repair (`_lookup_peer_unlocked` напрямую) → НЕ падает на
      ambiguity (best-effort сохранён) — тест-замечание/проверка.

### Task 5: upstream-citizenship note (АКЦЕНТ 4)
- [ ] В PR description: это general-correctness фикс repowire-mesh → кандидат на
      upstream-PR (fork vs upstream-citizenship, прецедент 4wuz). Отметить, не слать
      upstream в этом PR — решение по контрибуции за director/maintainer.

---

## DoD
- [ ] Релевантные suite зелёные: `pytest tests/test_circles.py tests/daemon/ -q` (+ новый файл).
- [ ] Полный repowire-fork pytest не регрессит (особенно test_circles, test_routes, hqvm/reverse-receipt тесты — не сломать Слой-1).
- [ ] lint/typecheck по конвенциям repo.
- [ ] PR против origin/main (repowire-fork), CI зелёный, English title/body + upstream-note.
- [ ] По готовности — notify backend-head с номером PR.
