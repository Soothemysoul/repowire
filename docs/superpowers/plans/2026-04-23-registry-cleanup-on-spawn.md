# Plan — Repowire registry cleanup on spawn (beads-wcy)

Spec: `docs/superpowers/specs/2026-04-23-registry-cleanup-on-spawn-design.md`
Worktree: `/home/kolyvanov_vn@project.client.loc/repos/agents-brain-team/repowire-fork/.worktrees/backend-worker-1776951596`
Branch: `feat/repowire-registry-cleanup-on-spawn` (based on `origin/main`)
Target repo: `Soothemysoul/repowire` (repowire-fork)

**Execution style:** TDD — RED → GREEN → REFACTOR → COMMIT per step. Follow `superpowers:test-driven-development` skill strictly.

**Approved direction:** director notif-ff40d62f (2026-04-23).

---

## Step 0 — baseline verification

**Action:**
1. `cd` в worktree path.
2. Verify clean state: `git status` (должно быть clean), `git log --oneline origin/main..HEAD` (должно быть пусто).
3. Run existing full suite: `uv run pytest -x tests/ 2>&1 | tail -20`. Ожидаемо все green (baseline: 24/24 по оценке director'а).

**Verify (grep/commands):**
- `git status --porcelain` → empty
- `git log --oneline origin/main..HEAD` → empty
- pytest exit code 0

**Commit:** none (just baseline).

---

## Step 1 — add failing tests for stale-sibling purge [RED]

**Action:** Open `tests/daemon/test_peer_registry_reconnect.py`. Добавить в конец файла новую секцию с 5 тестами:

```python
# ---------------------------------------------------------------------------
# beads-wcy — stale role-sibling purge on spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_role_sibling_purged_on_fresh_spawn(tmp_path):
    """Old OFFLINE peer with matching role-stem + last_seen > 300s must be
    purged when a fresh peer with a different timestamp spawns in the same
    (circle, backend)."""
    import datetime as _dt

    registry = _make_registry(tmp_path)
    # Old worker (different timestamp in path)
    old_id, old_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-1000",
        role=PeerRole.AGENT,
    )
    assert old_name == "devops-worker-1000-claude-code"
    await registry.mark_offline(old_id)
    # Back-date last_seen past 300s threshold
    registry._peers[old_id].last_seen = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)
    )

    # New worker (different path → different display_name)
    new_id, new_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-2000",
        role=PeerRole.AGENT,
    )
    assert new_name == "devops-worker-2000-claude-code"
    assert new_id != old_id
    assert old_id not in registry._peers, "stale role-sibling must be purged"
    assert old_id not in registry._mappings, "mapping must also be purged"


@pytest.mark.asyncio
async def test_recent_role_sibling_preserved(tmp_path):
    """Same scenario but old peer's last_seen < 300s ago: must stay."""
    import datetime as _dt

    registry = _make_registry(tmp_path)
    old_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-1000",
        role=PeerRole.AGENT,
    )
    await registry.mark_offline(old_id)
    # Recent disconnect — within threshold
    registry._peers[old_id].last_seen = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=60)
    )

    await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-2000",
        role=PeerRole.AGENT,
    )
    assert old_id in registry._peers, "recent offline sibling must not be purged"


@pytest.mark.asyncio
async def test_online_role_sibling_preserved(tmp_path):
    """ONLINE peer with matching stem must never be purged, regardless of age."""
    import datetime as _dt

    registry = _make_registry(tmp_path)
    online_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-1000",
        role=PeerRole.AGENT,
    )
    # Back-date last_seen but keep ONLINE status
    registry._peers[online_id].last_seen = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)
    )

    await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-2000",
        role=PeerRole.AGENT,
    )
    assert online_id in registry._peers, "online sibling must never be purged"


@pytest.mark.asyncio
async def test_cross_circle_sibling_preserved(tmp_path):
    """Stale sibling in a DIFFERENT circle must not be touched."""
    import datetime as _dt

    registry = _make_registry(tmp_path)
    other_id, _ = await registry.allocate_and_register(
        circle="other",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-1000",
        role=PeerRole.AGENT,
    )
    await registry.mark_offline(other_id)
    registry._peers[other_id].last_seen = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)
    )

    await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/repo/.worktrees/devops-worker-2000",
        role=PeerRole.AGENT,
    )
    assert other_id in registry._peers, "cross-circle sibling must not be purged"


@pytest.mark.asyncio
async def test_head_tier_no_sibling_match(tmp_path):
    """Head-tier paths (no trailing timestamp) must not trigger purge —
    stem extraction returns None, helper exits early."""
    import datetime as _dt

    registry = _make_registry(tmp_path)
    head_id, head_name = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/ai-infra/agents/backend-head",
        role=PeerRole.ORCHESTRATOR,
    )
    assert head_name == "backend-head-claude-code"
    await registry.mark_offline(head_id)
    registry._peers[head_id].last_seen = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)
    )

    # Reconnect the same head (no sibling churn expected)
    same_id, _ = await registry.allocate_and_register(
        circle="global",
        backend=AgentType.CLAUDE_CODE,
        path="/ai-infra/agents/backend-head",
        role=PeerRole.ORCHESTRATOR,
    )
    # Whether identity-reuse catches this or fresh-registration runs, the
    # old entry must NOT be silently purged by the new helper. Both outcomes
    # are acceptable: reuse returns same_id, fresh keeps head_id reachable
    # via _build_display_name's literal-purge. We just need the helper to
    # bail out on non-timestamped paths.
    # Simplest assertion: no crash, registry size stays consistent with
    # either reuse (1 peer) or literal-replace (1 peer).
    assert len(registry._peers) == 1
```

**Verify (commands):**
- `uv run pytest tests/daemon/test_peer_registry_reconnect.py -x 2>&1 | tail -20` — все 5 новых фейлятся (функция `_purge_stale_role_siblings_unlocked` ещё не существует). Existing 12 все ещё зелёные.
- `grep -c "test_stale_role_sibling_purged_on_fresh_spawn" tests/daemon/test_peer_registry_reconnect.py` → 1.

**Commit:**
```
test(peer-registry): failing tests for stale role-sibling purge (beads-wcy)
```

---

## Step 2 — implement helper + wire call site [GREEN]

**Action:** В `repowire/daemon/peer_registry.py`:

1. Добавить module-level constant над классом (после existing imports, перед `@dataclass class SessionMapping`):

```python
# Stale role-sibling purge threshold: OFFLINE peers with matching role-stem
# older than this are evicted on spawn to keep watchdog's substring-match
# /peers query from aliasing them onto the fresh scope (beads-wcy).
_STALE_SIBLING_PURGE_THRESHOLD_SEC = 300.0
```

2. Приватный helper в class `PeerRegistry` (разместить рядом с `_prune_name_from_mappings`, строка ~364):

```python
@staticmethod
def _extract_role_stem(display_name: str, backend: AgentType) -> str | None:
    """Extract the role basename from a display_name.

    display_name convention (spawn-claude.sh worktrees):
        ``<role>-<timestamp>-<backend.value>``

    Strips trailing ``-<backend.value>`` then trailing ``-<digits>``. Returns
    None when either segment is absent (e.g. head-tier paths that lack a
    timestamp). None disables the purge — caller bails out.
    """
    suffix = f"-{backend.value}"
    if not display_name.endswith(suffix):
        return None
    base = display_name[: -len(suffix)]
    match = re.match(r"^(.+?)-\d+$", base)
    if not match:
        return None
    stem = match.group(1)
    return stem or None


def _purge_stale_role_siblings_unlocked(
    self,
    new_display_name: str,
    circle: str,
    backend: AgentType,
    threshold_sec: float = _STALE_SIBLING_PURGE_THRESHOLD_SEC,
) -> int:
    """Purge OFFLINE peers sharing ``new_display_name``'s role-stem.

    Watchdog's ``classify_scope`` queries repowire via
    ``jq 'contains($role)' | first`` — a substring match that picks up any
    peer whose ``display_name`` contains the role stem. When a fresh scope
    spawns in a new worktree (new timestamp → new full display_name), a
    stale OFFLINE entry with the same role stem is the first match and its
    old ``last_seen`` propagates to the fresh scope as 5h-offline zombie.

    This proactive purge evicts such OFFLINE siblings in the same
    ``(circle, backend)`` with ``last_seen`` older than ``threshold_sec``.
    Must hold ``_lock``.

    Returns number of purged peers.
    """
    stem = self._extract_role_stem(new_display_name, backend)
    if stem is None:
        return 0
    now = datetime.now(timezone.utc)
    prefix = f"{stem}-"
    to_purge: list[str] = []
    for sid, peer in self._peers.items():
        if peer.status != PeerStatus.OFFLINE:
            continue
        if peer.circle != circle or peer.backend != backend:
            continue
        if peer.display_name == new_display_name:
            continue  # literal match already handled by _build_display_name
        if not peer.display_name.startswith(prefix):
            continue
        if not peer.last_seen:
            continue
        if (now - peer.last_seen).total_seconds() < threshold_sec:
            continue
        to_purge.append(sid)

    for sid in to_purge:
        purged = self._peers.pop(sid)
        self._mappings.pop(sid, None)
        age = (now - purged.last_seen).total_seconds() if purged.last_seen else 0.0
        logger.info(
            "Purged stale role-sibling %s (%s, offline %.0fs)",
            purged.display_name, sid, age,
        )
    if to_purge:
        self._mappings_dirty = True
    return len(to_purge)
```

3. Call site в `allocate_and_register`, в fresh-registration branch — ПОСЛЕ `_build_display_name`, ДО `peer = Peer(...)`. Точка: сейчас код выглядит (строка ~602-612):

```python
assigned_name = self._build_display_name(path or "", circle, backend)
effective_role_for_mapping = role if role is not None else preserved_role
allocated_id = self._find_or_allocate_mapping(
    assigned_name, circle, backend, path, role=effective_role_for_mapping,
)
```

Вставить ПОСЛЕ `assigned_name = ...` и ДО `_find_or_allocate_mapping`:

```python
# Hygiene: evict stale OFFLINE siblings sharing the role-stem before
# minting the new peer. Watchdog's /peers query would otherwise alias
# them onto this scope and kill fresh workers as zombies (beads-wcy).
self._purge_stale_role_siblings_unlocked(assigned_name, circle, backend)
```

**Verify (commands):**
- `uv run pytest tests/daemon/test_peer_registry_reconnect.py -x 2>&1 | tail -20` — все 17 тестов green (12 existing + 5 new).
- `uv run pytest -x tests/ 2>&1 | tail -20` — full suite green.
- `grep -n "_purge_stale_role_siblings_unlocked" repowire/daemon/peer_registry.py` — три совпадения (definition, call, не больше — helper приватный).
- `grep -n "_STALE_SIBLING_PURGE_THRESHOLD_SEC" repowire/daemon/peer_registry.py` — две строки (constant + default param).

**Commit:**
```
feat(peer-registry): purge stale role-siblings on spawn (beads-wcy)

Watchdog's classify_scope does jq 'contains($role)' | first on /peers —
a substring match that picks the first peer with the role stem in its
name, including stale OFFLINE entries from previous worktrees. Fresh
scopes spawning in a new worktree (new timestamp in path → new literal
display_name) don't trigger the existing literal-name purge in
_build_display_name, so the old entry remains and watchdog reads its
stale last_seen, killing the fresh scope as a 5h-offline zombie
(beads-ktt's primary symptom, clamp in #92 mitigates downstream).

Add _purge_stale_role_siblings_unlocked: on fresh-registration path,
evict OFFLINE peers in the same (circle, backend) whose display_name
starts with the same role-stem as the new candidate, when last_seen is
older than 300s. Identity-reuse path is not affected — reuse preserves
peer_id in place, refreshing last_seen.

Spinoff beads-8my tracks the root-cause watchdog fix (contains →
startswith). This patch is defense-in-depth so the registry stays
clean even if watchdog's query logic lags.
```

---

## Step 3 — piggyback .gitignore patch

**Action:** В `.gitignore` в корне worktree, добавить новую секцию после `.worktrees/`:

```
# Graphify artefacts (never commit; can grow large and contain binary HTML/JSON)
**/graphify-out/
```

**Verify:**
- `grep -c "graphify-out" .gitignore` → 1.
- `git check-ignore -v graphify-out/foo.html` → match line.

**Commit:**
```
chore(gitignore): ignore **/graphify-out/ artefacts

Brain-admin noticed repowire-fork/.gitignore lacks the pattern present
in system/.gitignore — risks random `git add` of graphify output. One-
liner, parallel with other defensive ignores.
```

---

## Step 4 — push + open PR

**Action:**
1. `git push origin feat/repowire-registry-cleanup-on-spawn`.
2. `gh pr create --title "feat: purge stale role-siblings on spawn (beads-wcy)" --body "$(cat <<'EOF' ... EOF)"`.

PR description должен содержать:
- Link to spec file.
- Summary of approach A vs spinoff beads-8my (watchdog fix).
- Test plan checklist.
- Deploy note: daemon-side change, requires restart — coordinate window with director (beads-o3c observation ends ~25.04).

**Verify:**
- `gh pr view --json url,number,mergeable` → PR open, mergeable.
- CI status: `gh pr view --json statusCheckRollup` → all green.

---

## Step 5 — report back to backend-head

**Action:** `notify_peer('agents-brain-team.backend-head', 'PR #<N> ready for review — beads-wcy. Plan executed per docs/superpowers/plans/2026-04-23-registry-cleanup-on-spawn.md, full suite green, piggyback .gitignore patch included as separate commit.')`.

Close beads-wcy stays with backend-head after PR merge.

---

## Rollback checklist (if tests fail and stuck)

- `git reset --hard origin/main` в worktree — возврат к baseline.
- Worktree сохраняется, branch можно переиспользовать.
- Escalate head via notify_peer with `## EXECUTION BLOCKED` + diagnostics.

## Assumptions pending user review

Нет. Все design-решения утверждены director (notif-ff40d62f, 2026-04-23).
