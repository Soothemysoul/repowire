<div align="center">
  <picture>
    <source srcset="https://raw.githubusercontent.com/prassanna-ravishankar/repowire/main/images/logo-dark.webp" media="(prefers-color-scheme: dark)" width="150" height="150" />
    <img src="https://raw.githubusercontent.com/prassanna-ravishankar/repowire/main/images/logo-light.webp" alt="Repowire Logo" width="150" height="150" />
  </picture>

  <h1>Repowire</h1>
  <p>Mesh network for AI coding agents - enables Claude Code and OpenCode sessions to communicate.</p>
</div>

## Installation

**Requirements:** macOS or Linux, Python 3.10+, tmux (for claudemux backend)

```bash
# Install from PyPI
uv tool install "repowire[claudemux]"
# or
pip install "repowire[claudemux]"
```

## Quick Start

```bash
# One-time setup - installs hooks, MCP server, and daemon service
repowire setup

# Verify everything is running
repowire status
```

Now start two Claude sessions in tmux:

```bash
# Terminal 1
tmux new-session -s dev -n frontend
cd ~/projects/frontend && claude

# Terminal 2 (or tmux split)
tmux new-window -t dev -n backend
cd ~/projects/backend && claude
```

The sessions auto-discover each other. In frontend's Claude:

```
"Ask backend what API endpoints they expose"
```

Claude uses the `ask_peer` tool, backend responds, and you get the answer back.

**What just happened?** See [How It Works: claudemux](#claudemux-default) for details.

## How It Works

### claudemux (default)

For Claude Code sessions running in tmux. This is the tested, production-ready backend.

#### What's Installed

| Component | Location | Purpose |
|-----------|----------|---------|
| **Daemon** | System service (launchd/systemd) | Routes messages between peers, runs on `127.0.0.1:8377` |
| **Hooks** | `~/.claude/settings.json` | SessionStart (register peer), SessionEnd (cleanup), Stop (capture responses) |
| **MCP Server** | Registered with Claude | Provides `ask_peer`, `list_peers`, `notify_peer`, `broadcast` tools |
| **Config** | `~/.repowire/config.yaml` | Peer registry and settings |
| **Logs** | `~/.repowire/daemon.log` | Daemon output |

<details>
<summary><strong>Architecture</strong></summary>

```
┌─────────────┐                           ┌─────────────┐
│  frontend   │    ask_peer("backend")    │   backend   │
│   Claude    │ ─────────────────────────►│   Claude    │
│             │                           │             │
│             │ ◄─────────────────────────│             │
│             │      response text        │             │
└─────────────┘                           └─────────────┘
       │                                         │
       │ MCP tool call                           │ Stop hook captures
       ▼                                         ▼ response from transcript
┌─────────────────────────────────────────────────────┐
│                      Daemon                          │
│                   127.0.0.1:8377                     │
│                                                     │
│  1. Receives query from frontend                    │
│  2. Looks up backend's tmux session                 │
│  3. Injects query into backend's pane (libtmux)    │
│  4. Waits for Stop hook to send response            │
│  5. Returns response to frontend                    │
└─────────────────────────────────────────────────────┘
```

**Why tmux?** Claude Code runs in a terminal. Tmux gives us programmatic access to inject queries (via `send_keys`) into another session's pane.

**Why hooks?** Claude Code doesn't have an API. Hooks are the only extension point:
- **SessionStart**: Registers peer with daemon, injects list of available peers into Claude's context
- **SessionEnd**: Marks peer offline, cancels pending queries immediately
- **Stop**: Reads transcript, extracts Claude's response, sends to daemon

**Why a central daemon?** Single source of truth for peer registry. Runs as a system service so it survives reboots and is always available when Claude sessions start.

</details>

---

### opencode (experimental)

> ⚠️ **Untested** - Architecture exists but not battle-tested.

For OpenCode sessions using the opencode-ai SDK.

#### What's Installed

| Component | Location | Purpose |
|-----------|----------|---------|
| **Daemon** | System service | Routes messages, `127.0.0.1:8377` |
| **Plugin** | OpenCode plugins directory | Provides peer tools |
| **Config** | `~/.repowire/config.yaml` | Peer registry |

<details>
<summary><strong>Architecture</strong></summary>

```
┌─────────────┐                           ┌─────────────┐
│   Peer A    │    ask_peer("B")          │   Peer B    │
│             │ ─────────────────────────►│  (OpenCode) │
│             │                           │             │
│             │ ◄─────────────────────────│             │
│             │      response             │             │
└─────────────┘                           └─────────────┘
       │                                         ▲
       │                                         │
       ▼                                         │
┌─────────────────────────────────────────────────────┐
│                      Daemon                          │
│                                                     │
│  • Calls OpenCode SDK directly                      │
│  • SDK returns response (no hooks needed)           │
└─────────────────────────────────────────────────────┘
```

**Why simpler?** OpenCode has an SDK with direct API access. No need for hooks or tmux - we can send messages and get responses programmatically.

</details>

To use: `repowire setup --backend opencode`

## CLI Reference

```bash
# Main commands
repowire setup                    # Install hooks, MCP server, daemon service
repowire setup --no-service       # Skip daemon service (run manually with 'serve')
repowire status                   # Show what's installed and running
repowire uninstall                # Remove all components

# Manual daemon control
repowire serve                    # Run daemon in foreground

# Peer commands
repowire peer list                # List peers and their status
repowire peer ask NAME "query"    # Test: ask a peer a question
```

<details>
<summary>Advanced commands (hidden from <code>--help</code>)</summary>

```bash
# Backend-specific
repowire claudemux status         # Check hooks installation
repowire opencode status          # Check plugin installation

# Service management
repowire service install          # Install daemon as system service
repowire service uninstall        # Remove system service
repowire service status           # Check service status

# Config
repowire config show              # Show current configuration
repowire config path              # Show config file path
```

</details>

## Advanced

### Multi-Machine Relay

> ⚠️ **Experimental** - Not production ready. `relay.repowire.io` is not yet available.

For Claude sessions on different machines:

```bash
# Self-host a relay server
repowire relay start --port 8000
repowire relay generate-key --user-id myuser

# On each machine, configure relay in ~/.repowire/config.yaml:
# relay:
#   enabled: true
#   url: "wss://your-relay-server:8000"
#   api_key: "your-key"
```

### Configuration Reference

Config file: `~/.repowire/config.yaml`

```yaml
daemon:
  host: "127.0.0.1"
  port: 8377
  backend: "claudemux"  # or "opencode"

relay:
  enabled: false
  url: "wss://relay.repowire.io"
  api_key: null

# Peers auto-populate via SessionStart hook
peers:
  frontend:
    name: frontend
    path: "/path/to/frontend"
    tmux_session: "dev:frontend"
    metadata:
      branch: "main"  # auto-detected from git
```

## License

MIT
