#!/usr/bin/env bash
# Atomic repowire-fork deploy (beads-n8pt):
#   reinstall -> restart daemon -> health-wait -> refresh-clients -> reap orphans
# Fail-fast: any stage failure aborts. Re-runnable (idempotent): refresh no-ops
# when daemon epoch unchanged; reaper is a no-op when nothing is orphan.
set -euo pipefail

REPO="${REPO:-$HOME/repos/agents-brain-team/repowire-fork}"
DAEMON_URL="${REPOWIRE_DAEMON_URL:-http://127.0.0.1:8377}"
SCOPE="${REFRESH_SCOPE:-workers}"
APPLY_REAP="${APPLY_REAP:-0}"   # 0 = dry-run reaper (default, safe)

log() { printf '[deploy %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

cd "$REPO"
SHA="$(git rev-parse --short HEAD)"
REASON="deploy repowire-fork sha=${SHA}"

# Capture currently installed version for rollback note.
PREV_VER="$(repowire --version 2>/dev/null || echo unknown)"
log "current installed version: ${PREV_VER}"

# 1) Reinstall from this checkout. --force overwrites the existing tool env,
#    --reinstall rebuilds deps (canonical form per CLAUDE.md). uv.lock is NOT a
#    pip-constraints file, so it cannot be passed to --constraints.
log "uv tool install --force --reinstall from ${REPO}"
uv tool install --force --reinstall "${REPO}"

# 2) Restart daemon.
log "restart repowire.service"
systemctl --user restart repowire

# 3) Health-wait (fail-fast if daemon does not come up).
log "waiting for daemon health"
for i in $(seq 1 30); do
  if curl -fsS "${DAEMON_URL}/health" >/dev/null 2>&1; then
    log "daemon healthy after ${i}s"; break
  fi
  if [ "$i" -eq 30 ]; then
    log "ERROR: daemon did not become healthy in 30s — ABORT. Rollback: see docs/runbook-repowire-deploy.md"
    exit 1
  fi
  sleep 1
done

# 4) Refresh live clients (contract rz1g). Token from env $REPOWIRE_AUTH_TOKEN
#    (backend-head notif-d800fdec); helper reads it by default, header omitted
#    if unset. No config parsing.
log "POST /control/refresh-clients scope=${SCOPE}"
python3 "${REPO}/scripts/repowire_refresh_clients.py" \
  --daemon-url "${DAEMON_URL}" --reason "${REASON}" --scope "${SCOPE}"

# 5) Reap orphan ws_hook/mcp procs (dry-run unless APPLY_REAP=1).
log "reaping orphans (apply=${APPLY_REAP})"
if [ "${APPLY_REAP}" = "1" ]; then
  python3 "${REPO}/scripts/repowire_reap_orphans.py" --apply
else
  python3 "${REPO}/scripts/repowire_reap_orphans.py"
fi

log "deploy complete: sha=${SHA}"
