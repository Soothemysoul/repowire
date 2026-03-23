# CLAUDE.md

## Build & Test

```bash
uv tool install --force --reinstall .   # install globally (hooks run from installed package!)
uv sync --extra dev                     # dev deps (pytest, ruff, ty, httpx-ws)
pytest                                  # 222 tests
ruff check repowire/                    # lint
uv run ty check repowire/              # type check
```

CI runs: ruff check, ty check, pytest (`.github/workflows/ci.yml`).

## Releasing

Update `version` in `pyproject.toml`, commit, tag, push:
```bash
git tag v0.X.Y && git push origin main --tags
```
CI triggers PyPI publish from tags.

## Architecture

```
MCP Server (mcp/server.py)          — thin HTTP client, delegates to daemon
    │
HTTP Daemon (daemon/app.py)         — FastAPI, :8377
    │
PeerRegistry (daemon/peer_registry.py) — single source of truth for peers + persistence
    │         │              │
MessageRouter  QueryTracker   WebSocketTransport
(routes msgs)  (correlation   (connection mgmt)
               IDs, locked)
```

**Key modules:**
- `daemon/peer_registry.py` — peer state, circle access, events, lazy_repair, ghost eviction
- `daemon/message_router.py` — routes queries/notifications/broadcasts via WebSocket
- `daemon/query_tracker.py` — correlation ID tracking, asyncio Futures (async-locked)
- `daemon/routes/` — HTTP endpoints (peers, messages, websocket, spawn, health)
- `hooks/` — Claude Code lifecycle (session, stop, prompt, notification, websocket_hook)
- `relay/server.py` — hosted relay at repowire.io (WebSocket bridge + HTTP tunnel)
- `mcp/server.py` — MCP tools (list_peers, ask_peer, notify_peer, broadcast, whoami, set_description, spawn_peer, kill_peer)

## Design Philosophy: Lazy Repair

Nothing polls. Work is deferred until needed, then piggy-backed on that request.

- **Liveness:** `lazy_repair()` runs max 1x/30s, triggered by MCP endpoints. Dead peers discovered when someone talks to them.
- **Persistence:** Disk writes debounced via dirty flags, flushed during lazy_repair or shutdown. Never on every mutation.
- **Hooks:** WebSocket hook is fully reactive. Daemon pings, hook pongs with liveness. No timers, no file watchers.
- **Rule:** Never add polling loops, periodic timers, or eager disk writes. Piggy-back on lazy_repair or the specific request.

## Hooks (Claude Code)

- **SessionStart** → registers peer, spawns ws-hook (flock dedup for sub-sessions), injects peer list as context
- **Stop** → extracts response + tool calls from transcript, posts chat turns via `/events/chat`, delivers query response via `/response` (with correlation_id from pending file, flock-protected)
- **UserPromptSubmit** → marks BUSY
- **Notification** (idle_prompt) → resets to ONLINE

State machine: `OFFLINE → ONLINE ↔ BUSY`

Key files: `session_handler.py`, `stop_handler.py`, `prompt_handler.py`, `notification_handler.py`, `websocket_hook.py`, `utils.py` (has `derive_display_name()`)

## Relay

Hosted at repowire.io. Daemon connects outbound via WSS. Cookie-based auth for browser dashboard access.

- `relay/server.py` — FastAPI relay (WS bridge + HTTP tunnel + SSE bridge + landing page)
- `relay/auth.py` — API key validation
- `daemon/relay_client.py` — outbound WSS with auto-reconnect, HTTP tunnel handler (strips proxy headers)
- Deploy: `.github/workflows/relay.yml` → GCR → Helm → GKE

## Config

File: `~/.repowire/config.yaml`

```yaml
daemon:
  host: "127.0.0.1"
  port: 8377
  auth_token: "optional"          # WebSocket auth
  prune_max_age_hours: 24         # evict offline peers older than this
  spawn:
    allowed_commands: [claude, claude --dangerously-skip-permissions]
    allowed_paths: [~/git, ~/projects]

relay:
  enabled: true
  url: "wss://repowire.io"
  api_key: "rw_..."               # auto-generated
```

Peers auto-register via WebSocket on session start — no manual config.

## Testing Notes

- Route tests: `httpx.AsyncClient` + `ASGITransport`, manually init deps (no lifespan)
- WebSocket tests: `httpx-ws` + `ASGIWebSocketTransport`
- PeerRegistry tests: override `_events_path` and clear `_events` to isolate from real data
- Hooks run from installed package — `uv tool install --force --reinstall .` after code changes

## Dashboard

- Next.js static export served by daemon at `localhost:8377/dashboard`
- Remote: served by relay at `repowire.io/dashboard` (cookie-authenticated tunnel)
- Events: 500-item circular buffer, persisted to `~/.repowire/events.json`
- Tool calls: stop hook extracts from transcript JSONL, included in `chat_turn` events
- Build: `repowire build-ui` or `cd web && npm run dev`
