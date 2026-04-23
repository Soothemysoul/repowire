# Design — Repowire registry cleanup on spawn (beads-wcy)

Author: backend-head (agents-brain-team) · Approved: director (notif-ff40d62f, 2026-04-23)

## Problem

`PeerRegistry.allocate_and_register` уже удаляет OFFLINE peer при **литеральном** совпадении `display_name` в `_build_display_name` (строки 313-319). Но watchdog при zombie-check делает substring-match на `/peers` JSON:

```bash
# brain-watchdog.sh:725-730
peer_status=$(jq -r --arg r "$role" \
  '[.peers[] | select(.name | contains($r))] | first | .status')
peer_last_seen=$(jq -r --arg r "$role" \
  '[.peers[] | select(.name | contains($r))] | first | .last_seen')
```

`$role` извлекается из scope name: `agent-devops-worker-<ts>.scope` → `devops-worker`. `contains` + `first` возвращает первый peer чьё `name` содержит `devops-worker` — независимо от online/offline.

**Сценарий false-positive kill:**

1. Worker peer регистрируется с path `/repo/.worktrees/devops-worker-1000` → display_name = `devops-worker-1000-claude-code`. Работает, уходит offline в t0.
2. Старый scope завершается, его registry-entry остаётся в `_peers` со status=OFFLINE, last_seen=t0.
3. Новый worker spawn'ится с path `/repo/.worktrees/devops-worker-2000` → display_name = `devops-worker-2000-claude-code`. **Разный literal display_name → existing purge не срабатывает**.
4. Watchdog `contains("devops-worker") + first` подхватывает **старый OFFLINE entry**, читает stale last_seen=t0. `classify_scope` видит offline_sec=5h → kill new scope как `offline-and-dead`.

`beads-ktt` (PR #92) зашил watchdog-side clamp (`offline_sec ≤ scope_age`), что устраняет primary symptom. Но root cause (substring match) остался, и stale entries засоряют registry до `_evict_stale_peers` (debounced 30s, max_age 72h по умолчанию).

## Decision

**Approach A (registry-side prefix purge):** на fresh-registration path — перед создания нового Peer — удалять OFFLINE peers в том же `(circle, backend)`, чей `display_name` начинается с тем же role-stem, что и новый candidate, если `last_seen > 300s`.

**Spinoff (beads-8my):** watchdog-side exact-match fix (`contains` → `startswith`) — отдельный эпик, devops-head.

### Рамки и rationale

| Решение | Значение |
|---|---|
| Threshold | 300s (5 min). Hardcoded module-level constant. Не config-exposed — defense-in-depth, не tuning knob. |
| Call site | `allocate_and_register`, после `_build_display_name`, до создания Peer. На identity-reuse path purge НЕ нужен: reuse сохраняет old peer_id. |
| Matching | Role-stem: `display_name` минус trailing `-<digits>` минус trailing `-<backend.value>`. Circle + backend **обязательно** совпадают. ONLINE peers — не трогаем. |
| Scope side-effects | Purge удаляет запись из `_peers` И `_mappings` (consistent с existing `_build_display_name:313-319`). `_mappings_dirty=True`. |
| Logging | `logger.info("Purged stale role-sibling: ...")` per entry. |

### Edge cases

- **Path без trailing timestamp** (head-tier роли: `/ai-infra/agents/backend-head` → display_name `backend-head-claude-code`). Stem extraction regex не матчит → helper возвращает 0 и ничего не делает. Heads не ротируют worktrees, sibling-а быть не может.
- **Role name сам заканчивается цифрами** (теоретически `worker-2`). Non-greedy regex `^(.+?)-\d+$` отъест только trailing ts, сохранит `worker-2`. Маловероятный сценарий в текущей кодовой базе.
- **Online peer с совпадающим stem**. Статус != OFFLINE → не трогаем (это singleton-conflict branch в `_build_display_name`, ему handler не нужен).
- **Tiny last_seen drift** (< 300s). Не stale → не purge. Защищает от racy disconnect/reconnect.

## Acceptance

Registry-side:

- [x] Constant `_STALE_SIBLING_PURGE_THRESHOLD_SEC = 300.0` в `repowire/daemon/peer_registry.py`.
- [x] Helper `_purge_stale_role_siblings_unlocked(new_display_name, circle, backend)` с docstring описывающим invariant.
- [x] Helper `_extract_role_stem(display_name, backend)` (или inline) — regex-based extraction с handled-None возвратом.
- [x] Call из `allocate_and_register` fresh-registration branch, после `_build_display_name`, до `Peer(...)` creation.

Tests (new scenarios в `tests/daemon/test_peer_registry_reconnect.py`):

- [x] `test_stale_role_sibling_purged_on_fresh_spawn` — OFFLINE `<role>-<ts1>-claude-code` с last_seen > 300s ago, new spawn `<role>-<ts2>-claude-code`. Old покинул `_peers` + `_mappings`.
- [x] `test_recent_role_sibling_preserved` — тот же сценарий, но last_seen < 300s. Old остался.
- [x] `test_online_role_sibling_preserved` — old peer ONLINE. Никогда не удаляется (даже если последующий insert коллайдит через `-2-` suffix logic).
- [x] `test_cross_circle_sibling_preserved` — разные circles. Покинуть нельзя (изоляция).
- [x] `test_cross_backend_sibling_preserved` — разные backends. Покинуть нельзя.
- [x] Existing 12 test scenarios в файле — NO regressions.

Piggyback (separate commit, same PR):

- [x] `.gitignore` в repowire-fork: добавить `**/graphify-out/` (brain-admin finding — отсутствует в отличие от system/).

Deploy:

- [ ] PR reviewed backend-head, merged.
- [ ] Deploy window координирует director (daemon-side change требует restart).

## Notes

Purge helper deliberately placed AFTER `_build_display_name`, не до. Причина: `_build_display_name` может менять candidate-имя через `-2-` suffix logic на случай singleton-collision с ONLINE peer. Stem нам нужен от финального `assigned_name`, не от пре-suffix base. Это также значит, что purge НЕ конкурирует с existing литерал-name purge — они работают на разных множествах OFFLINE peers (literal vs prefix).

Identity-reuse branches (`_try_reuse_by_identity_unlocked` sub-cases A/B) **не нуждаются** в purge: там old peer_id переиспользуется на месте, registry-entry обновляется in-place, stale last_seen замещается свежим `now`.
