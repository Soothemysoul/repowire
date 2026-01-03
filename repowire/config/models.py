"""Configuration models for Repowire."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class RelayConfig(BaseModel):
    """Configuration for relay server connection."""

    enabled: bool = Field(default=False, description="Whether to connect to relay")
    url: str = Field(default="wss://relay.repowire.io", description="Relay server URL")
    api_key: str | None = Field(None, description="API key for authentication")


class PeerConfig(BaseModel):
    """Configuration for a single peer."""

    name: str = Field(..., description="Human-readable peer name (folder name)")
    tmux_session: str | None = Field(None, description="Tmux session:window")
    path: str = Field(..., description="Working directory path")
    session_id: str | None = Field(None, description="Claude session ID (set by hooks)")


class DaemonConfig(BaseModel):
    """Configuration for the daemon process."""

    auto_reconnect: bool = Field(default=True, description="Auto-reconnect on disconnect")
    heartbeat_interval: int = Field(default=30, description="Heartbeat interval in seconds")
    socket_path: str = Field(default="/tmp/repowire.sock", description="Unix socket path for IPC")


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field(default="info", description="Log level")
    file: str | None = Field(None, description="Log file path")


class Config(BaseModel):
    """Main Repowire configuration."""

    relay: RelayConfig = Field(default_factory=RelayConfig)
    peers: dict[str, PeerConfig] = Field(default_factory=dict)  # keyed by peer name
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def get_config_dir(cls) -> Path:
        """Get the Repowire config directory."""
        return Path.home() / ".repowire"

    @classmethod
    def get_config_path(cls) -> Path:
        """Get the config file path."""
        return cls.get_config_dir() / "config.yaml"

    def save(self) -> None:
        """Save configuration to file."""
        config_dir = self.get_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)

        config_path = self.get_config_path()
        data = self.model_dump()

        with open(config_path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False)

    def add_peer(
        self,
        name: str,
        path: str,
        tmux_session: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Add or update a peer by name."""
        existing = self.peers.get(name)
        self.peers[name] = PeerConfig(
            name=name,
            tmux_session=tmux_session or (existing.tmux_session if existing else None),
            path=path,
            session_id=session_id or (existing.session_id if existing else None),
        )
        self.save()

    def update_peer_session(self, name: str, session_id: str) -> bool:
        """Update just the session_id for an existing peer."""
        if name in self.peers:
            self.peers[name].session_id = session_id
            self.save()
            return True
        return False

    def remove_peer(self, name: str) -> bool:
        """Remove a peer by name."""
        if name in self.peers:
            del self.peers[name]
            self.save()
            return True
        return False

    def get_peer(self, name: str) -> PeerConfig | None:
        """Get a peer by name."""
        return self.peers.get(name)

    def get_peer_by_tmux(self, tmux_session: str) -> PeerConfig | None:
        """Get a peer by tmux session:window."""
        for peer in self.peers.values():
            if peer.tmux_session == tmux_session:
                return peer
        return None


def load_config() -> Config:
    """Load configuration from file or create default."""
    config_path = Config.get_config_path()

    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        return Config(**data)

    # Create default config
    config = Config()

    # Check for environment overrides
    if relay_url := os.environ.get("REPOWIRE_RELAY_URL"):
        config.relay.url = relay_url
    if api_key := os.environ.get("REPOWIRE_API_KEY"):
        config.relay.api_key = api_key
        config.relay.enabled = True

    return config
