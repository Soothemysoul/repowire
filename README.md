<div align="center">
  <picture>
    <source srcset="https://raw.githubusercontent.com/prassanna-ravishankar/repowire/main/images/logo-dark.webp" media="(prefers-color-scheme: dark)" width="150" height="150" />
    <img src="https://raw.githubusercontent.com/prassanna-ravishankar/repowire/main/images/logo-light.webp" alt="Repowire Logo" width="150" height="150" />
  </picture>

  <h1>Repowire</h1>
  <p>Mesh network for AI coding agents (Claude Code, OpenCode) - enables sessions to communicate.</p>
</div>

## Installation

```bash
# Install from PyPI
uv tool install "repowire[claudemux]"
# or
pip install "repowire[claudemux]"
```

## Quick Start

```bash
# One-time setup (installs hooks, MCP server, and daemon service)
repowire setup

# Check status
repowire status

# Start Claude in tmux windows - peers auto-register via SessionStart hook
tmux new-window -n alice
cd ~/projects/frontend && claude

tmux new-window -n bob
cd ~/projects/backend && claude
```

The daemon runs as a system service (launchd on macOS, systemd on Linux) and starts automatically on login.

Alice and Bob can now talk:
```
# In Alice's Claude session:
"Ask bob what API endpoints they have"
```

## How It Works

```
┌─────────────┐                        ┌─────────────┐
│   Alice     │  ask_peer("bob", ...)  │    Bob      │
│  (claude)   │ ───────────────────►   │  (claude)   │
│             │                        │             │
│             │  ◄─────────────────    │             │
│             │   Stop hook captures   │             │
└─────────────┘   response & returns   └─────────────┘
        │                                     │
        └──────────┐           ┌──────────────┘
                   ▼           ▼
              ┌─────────────────────┐
              │   HTTP Daemon       │
              │  127.0.0.1:8377     │
              │                     │
              │  - routes queries   │
              │  - tracks pending   │
              │  - receives hooks   │
              └─────────────────────┘
```

1. **SessionStart hook** registers peer with metadata (name = folder name, git branch)
2. **SessionStart hook** injects peer context into Claude (lists available peers)
3. **ask_peer** sends query to daemon, daemon injects into target's tmux pane
4. **Target Claude** responds naturally
5. **Stop hook** fires at end of turn, captures response from transcript
6. **Response** routes back to caller via daemon

## Backends

Repowire supports two backends for different AI coding environments:

### claudemux (default)
For **Claude Code** sessions running in tmux.
- Peers auto-register via SessionStart hook
- Responses captured via Stop hook reading transcript
- Requires: tmux, Claude Code with hooks support

```bash
repowire setup --backend claudemux
repowire serve --backend claudemux
```

### opencode
For **OpenCode** sessions using the opencode-ai SDK.
- Responses returned directly from SDK (no hooks needed)
- Requires: OpenCode server running

```bash
repowire setup --backend opencode
repowire serve --backend opencode
```

Set default backend in config: `daemon.backend: "claudemux"` or `"opencode"`

## MCP Tools

| Tool | Description |
|------|-------------|
| `list_peers()` | List all registered peers and their status |
| `ask_peer(peer_name, query)` | Ask a peer a question, wait for response |
| `notify_peer(peer_name, message)` | Proactively share info (don't use for responses) |
| `broadcast(message)` | Send message to all peers (announcements only) |

Note: Peers auto-register via SessionStart hook. Your response to `ask_peer` queries is captured automatically - don't use `notify_peer` to respond.

## CLI Commands

```bash
# Main commands
repowire setup                    # Install everything (hooks, MCP, daemon service)
repowire setup --no-service       # Skip daemon service (use 'serve' manually)
repowire status                   # Show installation and daemon status
repowire uninstall                # Remove all components

# Daemon
repowire serve                    # Start daemon manually (foreground)
repowire serve --backend opencode # Start with specific backend

# Peer management
repowire peer list                # List peers and status
repowire peer ask NAME "query"    # Test: ask a peer a question
```

Advanced commands are available but hidden from help: `claudemux`, `opencode`, `service`, `config`, `relay`.

## Multi-Machine Setup

For Claude sessions on different machines, use the relay server:

### 1. Deploy relay (or use repowire.io)

```bash
# Self-hosted
repowire relay start --port 8000

# Or use hosted relay at relay.repowire.io
```

### 2. Generate API key

```bash
repowire relay generate-key --user-id myuser
# Save the generated key
```

### 3. Start daemon on each machine

```bash
# Configure relay in ~/.repowire/config.yaml, then:
repowire serve
```

## Configuration

Config file: `~/.repowire/config.yaml`

```yaml
daemon:
  host: "127.0.0.1"
  port: 8377
  backend: "claudemux"  # or "opencode"
  auto_reconnect: true
  heartbeat_interval: 30

relay:
  enabled: false
  url: "wss://relay.repowire.io"
  api_key: null

# Peers auto-populate via SessionStart hook
peers:
  frontend:
    name: frontend
    path: "/Users/you/app/frontend"
    tmux_session: "0:frontend"    # claudemux backend
    session_id: "abc123..."       # set by hook
    metadata:
      branch: "feat/new-ui"       # git branch, auto-detected
  backend:
    name: backend
    path: "/Users/you/app/backend"
    opencode_url: "http://localhost:4096"  # opencode backend
    session_id: "def456..."
    metadata:
      branch: "main"
```

## Testing the Flow

1. Run setup: `repowire setup`
2. Check status: `repowire status` (daemon should be running)
3. Create tmux windows for test peers
4. In each window: `cd ~/projects/<some-project> && claude`
5. Verify peers: `repowire peer list` - shows folder names as peer names
6. In one session: "Ask <peer-name> what this project does"

Note: Peer name = folder name, not tmux window name.

## Requirements

- macOS or Linux (service mode uses launchd/systemd)
- Python 3.10+
- tmux (for claudemux backend)
- Claude Code with hooks support (for claudemux backend)
- OpenCode (for opencode backend)

## License

MIT
