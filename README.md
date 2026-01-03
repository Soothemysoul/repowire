# Repowire

Mesh network for Claude Code sessions - enables AI agents to communicate.

## Quick Start

```bash
# One-time setup (installs hooks + MCP server)
repowire setup --dev  # use --dev for local development

# Start daemon
repowire daemon start

# Start Claude in tmux windows - peers auto-register via SessionStart hook
tmux new-window -n alice
cd ~/projects/frontend && claude

tmux new-window -n bob
cd ~/projects/backend && claude
```

That's it. Alice and Bob can now talk:
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
              │      Daemon         │
              │  /tmp/repowire.sock │
              │                     │
              │  - routes queries   │
              │  - tracks pending   │
              │  - cleans stale     │
              └─────────────────────┘
```

1. **SessionStart hook** registers peer (name = folder name, e.g., `frontend`)
2. **ask_peer** sends query to daemon → daemon injects into target's tmux pane
3. **Target Claude** responds naturally
4. **Stop hook** fires at end of turn, captures response from transcript
5. **Response** routes back to caller via daemon

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
# Peer management
repowire peer list                          # List peers and status
repowire peer register NAME -t TMUX -p PATH # Register a peer
repowire peer unregister NAME               # Remove a peer
repowire peer ask NAME "query"              # Test: ask a peer

# Hook management
repowire hooks install                      # Install Claude Code hooks
repowire hooks uninstall                    # Remove hooks
repowire hooks status                       # Check installation

# Daemon (for relay mode)
repowire daemon start --relay-url URL       # Start daemon

# Relay server (self-hosted)
repowire relay start --port 8000            # Start relay server
repowire relay generate-key                 # Generate API key

# Configuration
repowire config show                        # Show current config
repowire config path                        # Show config file path
```

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
repowire daemon start \
  --relay-url wss://relay.repowire.io \
  --api-key rw_xxx
```

## Configuration

Config file: `~/.repowire/config.yaml`

```yaml
relay:
  enabled: false
  url: "wss://relay.repowire.io"
  api_key: null

# Peers auto-populate via SessionStart hook
peers:
  frontend:
    name: frontend
    tmux_session: "0:frontend"
    path: "/Users/you/app/frontend"
    session_id: "abc123..."  # set by hook
  backend:
    name: backend
    tmux_session: "0:backend"
    path: "/Users/you/app/backend"
    session_id: "def456..."

daemon:
  auto_reconnect: true
  heartbeat_interval: 30
```

## Testing the Flow

Use tmux MCP to set up test peers:

1. Start daemon: `repowire daemon start &`
2. Create windows for alice and bob via `tmux-mcp create-window`
3. In each window, run: `cd ~/development/projects/<some-project> && claude`
4. Verify with `repowire peer list` - peers show as folder names (e.g., `a2a-chat`)
5. In alice's session: "Ask a2a-chat what this project does"
6. Clean up: kill the tmux windows via `tmux-mcp kill-window`

Note: Peer name = folder name, not tmux window name.

## Requirements

- Python 3.10+
- tmux
- Claude Code with hooks support

## License

MIT
