# beads-jj7l — daemon registry persist/liveness: design-spec

- **Дата:** 2026-06-27
- **Автор:** backend-worker
- **Бид:** beads-jj7l (EPIC, repo=repowire-fork) · КОРЕНЬ beads-s8di · комплементарно beads-wi7y
- **Статус:** DRAFT — на checkpoint у backend-head (daemon-CORE, осторожно)
- **Gate для:** telegram-display rollout (финальное окно = daemon restart = mass-reconnect)
- **Изоляция:** только daemon-side. Client-side (детект+retry+alert) покрыт wi7y (уже отгружен).

---

## 1. Проблема

После рестарта демона живые сессии исчезают из mesh-реестра до тех пор, пока их WS-hook
не реконнектится. Если reconnect не успел/не случился, сессия жива, но `GET /peers` её не
показывает → внешний watchdog (`no_peer_record.go`) считает её orphan и сносит на 60-й минуте
(s8di: backend-head осиротел на 98 минут).

wi7y закрыл **детект+retry+alert на стороне клиента** (post-connect register-verify в WS-hook).
jj7l закрывает **предотвращение потери на стороне демона**: сделать так, чтобы факт регистрации
и liveness переживали рестарт, а `GET /peers` показывал переживших — чтобы watchdog не рипал
живых в окне reconnect. wi7y §3 прямо передал сюда «новую персистентную liveness-state».

---

## 2. Root cause (systematic-debugging, Phase 1 — подтверждено по коду)

Факты A/B/C из wi7y-спеки (§2) подтверждены и не повторяются здесь дословно. Углубление jj7l:

### Факт D — `_peers` НЕ регидрируется при старте, только лениво на reconnect
`app.py` lifespan (L86-93): конструирует `PeerRegistry` (→ `_load_mappings`) и зовёт
`prune_offline`. **Регидрации `_peers` из `_mappings` нет.** `_peers` восстанавливается ТОЛЬКО
лениво — `_try_reuse_by_identity_unlocked` **sub-case B** (`peer_registry.py:751-794`,
«mapping restore») создаёт `Peer` обратно в `_peers` **в момент reconnect'а WS-hook'а**. До
reconnect'а `_peers` по этому peer'у пуст → `GET /peers` его не отдаёт → `HasPeer=false`.

### Факт E — семантика внешнего watchdog (журнал s8di, `no_peer_record_test.go`)
Кандидат на reap: **`isNoPeerCandidate = PIDAlive && !HasPeer`**. То есть **любая запись в
`GET /peers` (даже OFFLINE) ⇒ `HasPeer=true` ⇒ НЕ кандидат**, alert не шлётся, scope не рипается.
Конфиг: `NoPeerEarlyAlertSeconds=600` (early-alert), auto-kill backstop = 3600s. Отдельный
путь `zombie_offline_threshold_seconds=3600` рипает OFFLINE-зомби спустя 60 мин.

> **Инженерный вывод (определяет дизайн):** достаточно, чтобы при рестарте демона переживший
> peer **присутствовал в `GET /peers` хоть как OFFLINE** — это уже снимает false-positive
> `no_peer_record`. Живой peer реконнектится за секунды (WS `ConnectionClosed` срабатывает
> мгновенно при рестарте, + wi7y forced-reconnect) → задолго до zombie-offline порога (3600s).
> Мёртвый peer останется OFFLINE и будет корректно сорван zombie-offline на 60 мин. Обе ветки
> верны без новых статусов.

### Факт F — `SessionMapping` не хранит liveness; `prune_offline` судит по `updated_at`
`SessionMapping` (L73-86): `session_id, display_name, circle, backend, path, role, updated_at`.
Нет `last_seen`/`status`. `_load_mappings` (L184-210) отсеивает лишь по `Path(path).exists()`
(слабый прокси — worktree живёт дольше сессии). `prune_offline`/`_is_stale` (L1730-1736) судит
о «давно мёртв» по `updated_at`, который обновляется только при мутации mapping
(register/reuse/role), **НЕ на каждое сообщение** → длинная живая сессия, которая не
ре-регалась, имеет устаревший `updated_at` и была бы ошибочно отсеяна. Для точного DoD#3
нужно персистить реальный `last_seen`.

---

## 3. DoD (из бида)

1. **Персистентная liveness переживает рестарт** — факт регистрации/last_seen восстанавливается
   БЕЗ требования немедленного reconnect; ЛИБО reconnect-grace (peer не reaped/не orphan в
   grace-окне после рестарта).
2. **Надёжный re-register на reconnect независимо от hook-версии** (закрывает hook-version gap
   wi7y со стороны демона).
3. **Без false-positive на давно мёртвых mappings** — различать «был жив, должен реконнектнуться»
   vs «давно мёртв».

---

## 4. Архитектурный выбор (взвешивание — главное на checkpoint)

Все три варианта в итоге обязаны сделать одно и то же ядро: **чтобы `GET /peers` после рестарта
показывал переживших (`HasPeer=true`)**, нужно регидрировать `_peers` из персистентного
состояния. Различие — в статусе/grace-семантике регидрированных peer'ов.

### Вариант A — Persist-liveness + регидрация как OFFLINE (fresh clock)
Добавить `last_seen` в `SessionMapping`; при старте после `prune_offline` регидрировать `_peers`
из переживших mappings со `status=OFFLINE`, `last_seen=offline_since=restart-time`.
- **Плюсы:** минимум — никакого нового enum/статуса; опирается на `HasPeer=true`; честный
  статус (нет живого транспорта = не ONLINE). Совместимо с существующей sub-case B.
- **Минусы:** sub-case A переиспользует OFFLINE-peer только в TTL=120s/singleton/RESTARTING —
  поздний reconnect (>120s) уходит в sub-case B, где `peer_in_memory=True` (после регидрации!)
  → возврат None → fresh-path. Нужно гарантировать, что fresh-path не плодит churn имени/id.

### Вариант B — Reconnect-grace через статус RESTARTING (или новый GRACE)
Регидрировать как `RESTARTING`: sub-case A уже **безусловно** переиспользует RESTARTING (L733),
а `liveness_tick` демотит «застрявший» RESTARTING после cap (900s) → OFFLINE → обычная эвикция.
- **Плюсы:** переиспользует готовую restart-машинерию (reuse + cap-демоут) ровно под смысл
  «демон рестартанул, peer должен вернуться».
- **Минусы:** семантическая перегрузка — RESTARTING сейчас = *self-restart peer'а* (context
  overflow) с **hold-queue** удержанием notify'ев. Для daemon-restart удерживать notify'и
  регидрированному (возможно мёртвому) peer'у неверно; плюс RESTARTING иначе рисуется в
  dashboard/TSV. Риск спутать два разных «restarting».

### Вариант C — Hybrid (РЕКОМЕНДАЦИЯ)
Persist `last_seen` (как A) + регидрация как **OFFLINE с fresh clock** (как A), НО явно
гарантировать **reconnect-grace при reuse**: регидрированный peer переиспользуется sub-case A
**безусловно в пределах grace-окна** (не упираясь в 120s TTL), как это сделано для singleton/
RESTARTING — через лёгкий **транзиентный (не персистируемый) маркер регидрации**. DoD#3 —
`prune_offline`-on-load по **персистентному `last_seen`** (а не `updated_at`).
- **Плюсы:** durable liveness (DoD#1) + надёжный reuse для всех hook-версий (DoD#2, см. §5.3) +
  точный отсев давно мёртвых (DoD#3) — **без** перегрузки RESTARTING и без нового публичного
  статуса. Закрывает минус варианта A (churn на позднем reconnect).
- **Минусы:** маленькое дополнение к sub-case A (учесть маркер регидрации в условии reuse).

**Рекомендация воркера: Вариант C.** Минимальная новизна на daemon-CORE, опора на проверенную
sub-case-B логику, не трогает hold-queue/RESTARTING-семантику. Финальное решение — за head.

---

## 5. Предлагаемое решение (Вариант C, детально)

Все изменения — `repowire/daemon/peer_registry.py` (+ вызов в `app.py`).

### 5.1 `SessionMapping`: добавить `last_seen`
```python
@dataclass
class SessionMapping:
    ...
    updated_at: str | None = None
    last_seen: str | None = None   # NEW: ISO-8601, реальная liveness (для prune + rehydrate)
```
- Заполняется при персисте: в `_persist_mappings` (или перед ним) пройти `_peers` и снять
  `peer.last_seen` в соответствующий `mapping.last_seen`. Персист и так дебаунсится
  (`lazy_repair` 1×/30s + shutdown) — Lazy-Repair соблюдён, новых таймеров нет.
- Обратная совместимость: поле опционально; старый `sessions.json` без `last_seen` грузится
  (default None) → при регидрации fallback на `updated_at`, затем на restart-time.

### 5.2 Регидрация `_peers` при старте
Новый метод `rehydrate_from_mappings()`, вызывается в `start()` (или в lifespan сразу после
`prune_offline`). Для каждого пережившего mapping создать `Peer`:
```
status        = OFFLINE                  # нет живого транспорта — честно
last_seen     = restart-time             # свежие «часы» (см. §5.3 про reuse-grace)
offline_since = restart-time             # свежее grace-окно для q51-takeover / zombie
pane_id       = None                     # реальный pane вернётся на reconnect
role/circle/backend/path/display_name    = из mapping
metadata["_rehydrated"] = True           # ТРАНЗИЕНТНЫЙ маркер (НЕ персистится)
```
Эффект: сразу после рестарта `GET /peers` отдаёт пережившего как OFFLINE ⇒ `HasPeer=true` ⇒
`no_peer_record` не рипает (Факт E). DoD#1 ✓.

### 5.3 Reuse регидрированного peer'а на reconnect (DoD#2)
В `_try_reuse_by_identity_unlocked` **sub-case A** добавить регидрированный peer к условиям
безусловного reuse (рядом с singleton/RESTARTING), ограниченного reconnect-grace-окном:
```python
reuse = (
    peer.status == PeerStatus.RESTARTING
    or peer.metadata.get("_rehydrated")        # NEW: regdrated после рестарта демона
    or self._is_singleton_role(path)
    or age <= reconnect_ttl
)
```
При reuse — снять `_rehydrated` маркер, `status=ONLINE`, привязать pane/tmux. Любая hook-версия
шлёт register c (path, circle, backend) → reuse одного и того же peer_id → нет churn имени/«-2».
Это и есть «надёжный re-register независимо от hook-версии» со стороны демона. DoD#2 ✓.

> Без §5.3 (чистый вариант A): поздний (>120s) reconnect ушёл бы в sub-case B, где после
> регидрации `peer_in_memory=True` → None → fresh-path → потенциальный churn. §5.3 это
> устраняет малой правкой.

### 5.4 DoD#3 — отсев «давно мёртв» + bootstrap-leniency (РЕФАЙНМЕНТ, notif-c0eceae1)

`prune_offline`-on-load судит по **`last_seen`** (реальная liveness), НЕ по `updated_at`
(Факт F: `updated_at` двигается только на мутацию mapping → ненадёжен). Новый `_is_stale`:

- **`last_seen` ОТСУТСТВУЕТ → bootstrap-lenient: НЕ stale (keep, регидрировать).** Mapping без
  `last_seen` — из до-jj7l-эпохи (**активационный restart**: старый демон никогда не писал
  `last_seen`). Liveness неизвестна → еррим В СТОРОНУ регидрации, НЕ прунить по `updated_at`.
  **Обоснование (асимметрия рисков):** ложно-сохранённый МЁРТВЫЙ peer само-заживает
  (регидрирован OFFLINE → zombie-offline reap 60мин / `_evict_stale_peers` после max-age);
  ложно-спруненный ЖИВОЙ peer = orphan — ровно баг, ради которого jj7l. Это безопасность
  САМОГО активационного окна: живая сессия не должна стать orphan на том restart'е, что
  поставляет jj7l. Срабатывает ТОЛЬКО когда `last_seen` absent.
- **`last_seen` есть и старше `prune_max_age_hours` (24h) → stale (прунится).** Обычный
  age-prune — действует после ПЕРВОГО jj7l-restart, когда `last_seen` уже персистится
  (`_snapshot_liveness` снимает `peer.last_seen` в mapping при flush).
- **`last_seen` есть, но не парсится → corrupt → stale.** Отличается от absent-кейса.

Регидрированные-но-мёртвые: остаются OFFLINE, не реконнектятся → (a) zombie-offline watchdog
рипает на 3600s; (b) `_evict_stale_peers` удаляет, когда `last_seen`(restart-time)+`prune_max_age`
истёк. Само-заживает, ложного «alive» нет (OFFLINE, не ONLINE). DoD#3 ✓.

> Контракт `prune_offline` намеренно изменён (prune по `last_seen`, не `updated_at`). Три теста
> в `tests/test_session_mapper.py` переведены на `last_seen`-fixture (intent сохранён). Новый
> `test_activation_restart_rehydrates_all_without_last_seen` фиксирует безопасность окна.

### 5.5 Lazy-Repair соблюдён
Регидрация — **однократное действие на старте**, не таймер/loop. Снятие `last_seen` в mapping —
в уже дебаунсенном `_persist_mappings`. «Nothing polls» не нарушено.

---

## 6. Edge cases / взаимодействия (daemon-CORE — осторожно)

- **Takeover (q51):** регидрированный OFFLINE с `offline_since=restart-time` НЕ может быть
  немедленно отобран по имени — порог `_MIN_OFFLINE_SECONDS_FOR_TAKEOVER=30s` отсчитывается от
  рестарта, даёт живому peer'у фору реконнектнуться. ✓
- **`_evict_stale_peers` / `_purge_stale_role_siblings_unlocked`:** оба смотрят на `last_seen` и
  порог (prune_max_age / 300s). Свежий restart-time-clock защищает регидрированного от
  немедленной эвикции/purge. ✓
- **`_demote_disconnected_peers` / `liveness_tick`:** регидрированный уже OFFLINE (не ONLINE/BUSY)
  → не попадает под demote-ghost. `resurrected`-ветка `liveness_tick` промоутит его в ONLINE,
  только если транспорт реально подключён — для регидрированного без reconnect это не сработает
  (нет WS). ✓
- **`get_peer_by_pane`:** `pane_id=None` у регидрированного → не отдаётся по pane до реального
  reconnect (который привяжет pane). Исключает мис-роутинг на устаревший pane. ✓
- **Сервис-пиры (beads-7ijt):** уже персистятся offline и так; регидрация согласуется с этим
  (они и должны быть в `GET /peers`). Проверить, что не дублируется с существующей логикой.

---

## 7. План тестирования (TDD, Phase 3)

Новый `tests/daemon/test_registry_persist_liveness.py` (+ возможно расширить
`test_singleton_ghost_takeover.py`). Сначала падающие тесты:

1. `test_session_mapping_persists_last_seen` — после активности + `_persist_mappings`,
   `sessions.json` содержит `last_seen`; повторная загрузка его читает.
2. `test_rehydrate_populates_peers_as_offline` — новый registry с непустым `sessions.json` →
   после `start()`/rehydrate `get_all_peers()` отдаёт peer'ов как OFFLINE (≡ `HasPeer=true`).
3. `test_rehydrated_peer_reused_on_reconnect_preserves_peer_id` — после регидрации
   `allocate_and_register(path,circle,backend)` (без peer_id) возвращает ТОТ ЖЕ peer_id,
   статус ONLINE, без «-2» суффикса — даже при `age>120s` (grace-маркер).
4. `test_rehydrate_skips_long_dead_mappings` — mapping с `last_seen` старше prune_max_age НЕ
   регидрируется (DoD#3).
5. `test_rehydrated_dead_peer_evicted_after_max_age` — регидрированный, который не реконнектится,
   эвиктится `_evict_stale_peers`/`lazy_repair` после prune_max_age.
6. `test_rehydrate_backward_compat_no_last_seen` — старый `sessions.json` без `last_seen`
   грузится и регидрируется (fallback) без ошибок.
7. `test_rehydrated_offline_not_immediately_taken_over` — имя регидрированного не отбирается до
   `_MIN_OFFLINE_SECONDS_FOR_TAKEOVER` от рестарта.

Регрессия: весь `pytest` зелёный (текущие 222–231), `ruff check repowire/`,
`uv run ty check repowire/`. Hooks из установленного пакета — `uv tool install --force --reinstall .`
если затронуты hook-зависимые пути (здесь — daemon-only, вероятно не нужно).

---

## 8. Open questions для checkpoint (нужно решение backend-head)

1. **Главное — вариант:** утверждаем **C (hybrid: persist last_seen + rehydrate OFFLINE +
   reuse-grace маркер)**? Или предпочитаешь B (RESTARTING-reuse, переиспользует машинерию, но
   перегружает семантику)? Или строгий A (минимум, с риском churn на позднем reconnect)?
2. **Статус регидрированного:** OFFLINE (рекомендация — честно, `HasPeer=true` достаточно) vs
   новый публичный статус (напр. `RECONNECTING`) для наблюдаемости в dashboard? Новый статус =
   churn по TSV/dashboard/takeover-логике — рекомендую НЕ вводить, если нет явной нужды.
3. **Куда поместить prune+rehydrate:** консолидировать в `start()` (тогда и test-lifespan
   получит prune+rehydrate — паритет, но меняет текущее тест-поведение) vs оставить
   `prune_offline` в lifespan и добавить только `rehydrate` после него? Рекомендую
   консолидацию в `start()`.
4. **Маркер регидрации:** `metadata["_rehydrated"]` (транзиентный, ноль churn схемы) vs
   отдельное непубличное поле `Peer`? Рекомендую metadata-маркер (проще, не персистится).
5. **Подтверждение Факта E:** семантика watchdog (`HasPeer=true` при OFFLINE достаточно; отдельно
   zombie-offline 3600s) взята из журнала s8di / `no_peer_record_test.go` — подтверждаешь, что
   это актуальная семантика прод-watchdog? От этого зависит «OFFLINE достаточно».

---

## 9. Риски

- **session-lifecycle / reconnect — осторожно (daemon-CORE).** Регидрация не должна ломать
  happy-path `allocate_and_register`/transport/существующую регистрацию. Изоляция: только
  стартовая регидрация + sub-case A reuse-условие + поле mapping. Полный pytest — обязательный
  гейт.
- **Мёртвые peer'ы временно видны OFFLINE** до prune_max_age. Это корректно (они БЫЛИ
  зарегистрированы) и ограничено `prune_offline`-on-load (24h) + zombie-offline (60мин). Не
  ложное «alive».
- **Согласованность `last_seen` в mapping** — снимок при дебаунс-персисте, т.е. отставание до
  ~30s/до shutdown. Для prune-решения (порог 24h) погрешность 30s незначима.
- **Раскат jj7l = daemon restart** (= финальное окно telegram-display, координирует директор).
  Тестировать на регрессию реестра перед окном.
```
