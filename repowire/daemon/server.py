"""Repowire daemon - central message router for local mesh."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import libtmux

from repowire.config.models import Config, load_config
from repowire.protocol.peers import Peer, PeerStatus

SOCKET_PATH = Path("/tmp/repowire.sock")
PID_FILE = Path.home() / ".repowire" / "daemon.pid"
PENDING_DIR = Path.home() / ".repowire" / "pending"
CLEANUP_INTERVAL = 60  # seconds


class DaemonClient:
    """A connected MCP client."""

    def __init__(
        self,
        client_id: str,
        peer_name: str | None,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.client_id = client_id
        self.peer_name = peer_name
        self.writer = writer
        self.pending_queries: dict[str, asyncio.Future[str]] = {}


class RepowireDaemon:
    """Central daemon that routes messages between peers."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        self.tmux_server = libtmux.Server()
        self.machine = socket.gethostname()

        self._clients: dict[str, DaemonClient] = {}
        self._peer_to_client: dict[str, str] = {}  # peer_name -> client_id
        self._socket_server: asyncio.Server | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the daemon."""
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_DIR.mkdir(parents=True, exist_ok=True)

        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()

        self._socket_server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(SOCKET_PATH),
        )
        self._running = True

        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))

        print(f"Daemon started on {SOCKET_PATH}")

    async def stop(self) -> None:
        """Stop the daemon."""
        self._running = False

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        for client in self._clients.values():
            for future in client.pending_queries.values():
                if not future.done():
                    future.cancel()
            try:
                client.writer.close()
                await client.writer.wait_closed()
            except Exception:
                pass

        self._clients.clear()
        self._peer_to_client.clear()

        if self._socket_server:
            self._socket_server.close()
            await self._socket_server.wait_closed()

        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        if PID_FILE.exists():
            PID_FILE.unlink()

    async def run_forever(self) -> None:
        """Run until signaled to stop."""
        stop_event = asyncio.Event()

        def handle_signal() -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal)

        # Start periodic cleanup task
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

        # Reload config and do initial cleanup
        self._cleanup_stale_peers()
        peer_names = list(self.config.peers.keys())
        print(f"Daemon ready (peers: {peer_names})")

        await stop_event.wait()
        await self.stop()

    async def _periodic_cleanup(self) -> None:
        """Periodically clean up stale peers."""
        while self._running:
            await asyncio.sleep(CLEANUP_INTERVAL)
            removed = self._cleanup_stale_peers()
            if removed:
                print(f"Cleaned up stale peers: {removed}")

    def _cleanup_stale_peers(self) -> list[str]:
        """Remove peers whose tmux session/window no longer exists."""
        self.config = load_config()
        stale = []

        for name, peer in list(self.config.peers.items()):
            if peer.tmux_session:
                status = self._get_peer_status(peer.tmux_session)
                if status == PeerStatus.OFFLINE:
                    stale.append(name)

        for name in stale:
            self.config.remove_peer(name)

        return stale

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a connected client."""
        client_id = str(uuid4())[:8]
        client = DaemonClient(client_id, None, writer)
        self._clients[client_id] = client

        try:
            while self._running:
                data = await reader.readline()
                if not data:
                    break

                try:
                    message = json.loads(data.decode())
                    await self._handle_message(client, message)
                except json.JSONDecodeError:
                    await self._send(client, {"error": "Invalid JSON"})
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if client.peer_name and client.peer_name in self._peer_to_client:
                del self._peer_to_client[client.peer_name]
            del self._clients[client_id]

    async def _send(self, client: DaemonClient, message: dict[str, Any]) -> None:
        """Send a message to a client."""
        try:
            data = json.dumps(message).encode() + b"\n"
            client.writer.write(data)
            await client.writer.drain()
        except Exception:
            pass

    async def _handle_message(self, client: DaemonClient, msg: dict[str, Any]) -> None:
        """Route a message from a client."""
        msg_type = msg.get("type")

        if msg_type == "register":
            client.peer_name = msg.get("peer_name")
            if client.peer_name:
                self._peer_to_client[client.peer_name] = client.client_id
            await self._send(client, {"type": "registered", "client_id": client.client_id})

        elif msg_type == "list_peers":
            # Reload config and clean up stale peers
            self._cleanup_stale_peers()
            peers = self._get_peers()
            await self._send(client, {"type": "peers", "peers": [p.to_dict() for p in peers]})

        elif msg_type == "query":
            await self._handle_query(client, msg)

        elif msg_type == "notify":
            await self._handle_notify(client, msg)

        elif msg_type == "broadcast":
            await self._handle_broadcast(client, msg)

        elif msg_type == "hook_response":
            await self._handle_hook_response(msg)

    def _tmux_to_filename(self, tmux_session: str) -> str:
        """Convert tmux session:window to safe filename."""
        # Replace : with _ for filesystem safety
        return tmux_session.replace(":", "_").replace("/", "_")

    async def _handle_query(self, client: DaemonClient, msg: dict[str, Any]) -> None:
        """Handle a query from a peer."""
        to_peer = msg.get("to_peer")
        text = msg.get("text", "")
        correlation_id = msg.get("correlation_id") or str(uuid4())
        from_peer = client.peer_name or "unknown"
        timeout = msg.get("timeout", 120.0)

        # Reload config to get fresh peer info
        self.config = load_config()

        # Find peer by name
        peer_config = self.config.get_peer(to_peer)
        if not peer_config:
            await self._send(client, {
                "type": "response",
                "correlation_id": correlation_id,
                "error": f"Unknown peer: {to_peer}",
            })
            return

        if not peer_config.tmux_session:
            await self._send(client, {
                "type": "response",
                "correlation_id": correlation_id,
                "error": f"Peer {to_peer} has no tmux session",
            })
            return

        pane = self._get_peer_pane(peer_config.tmux_session)
        if not pane:
            await self._send(client, {
                "type": "response",
                "correlation_id": correlation_id,
                "error": f"Peer {to_peer} is offline",
            })
            return

        # Create pending file keyed by tmux target (stable across session restarts)
        pending_filename = self._tmux_to_filename(peer_config.tmux_session)
        pending_file = PENDING_DIR / f"{pending_filename}.json"
        pending_data = {
            "correlation_id": correlation_id,
            "from_peer": from_peer,
            "to_peer": to_peer,
            "tmux_session": peer_config.tmux_session,
            "query": text,
            "timestamp": datetime.utcnow().isoformat(),
        }
        pending_file.write_text(json.dumps(pending_data))

        response_future: asyncio.Future[str] = asyncio.Future()
        client.pending_queries[correlation_id] = response_future

        formatted_query = f"@{from_peer} asks: {text}"
        pane.send_keys(formatted_query, enter=True)

        try:
            response = await asyncio.wait_for(response_future, timeout=timeout)
            await self._send(client, {
                "type": "response",
                "correlation_id": correlation_id,
                "text": response,
            })
        except asyncio.TimeoutError:
            await self._send(client, {
                "type": "response",
                "correlation_id": correlation_id,
                "error": f"Timeout waiting for {to_peer}",
            })
        finally:
            client.pending_queries.pop(correlation_id, None)
            pending_file.unlink(missing_ok=True)

    async def _handle_notify(self, client: DaemonClient, msg: dict[str, Any]) -> None:
        """Handle a notification."""
        to_peer = msg.get("to_peer")
        text = msg.get("text", "")
        from_peer = client.peer_name or "unknown"

        # Reload config
        self.config = load_config()

        peer_config = self.config.get_peer(to_peer)
        if not peer_config:
            await self._send(client, {"type": "error", "error": f"Unknown peer: {to_peer}"})
            return

        if not peer_config.tmux_session:
            await self._send(client, {"type": "error", "error": f"Peer {to_peer} has no tmux session"})
            return

        pane = self._get_peer_pane(peer_config.tmux_session)
        if not pane:
            await self._send(client, {"type": "error", "error": f"Peer {to_peer} is offline"})
            return

        formatted_message = f"@{from_peer} says: {text}"
        pane.send_keys(formatted_message, enter=True)
        await self._send(client, {"type": "ok"})

    async def _handle_broadcast(self, client: DaemonClient, msg: dict[str, Any]) -> None:
        """Handle a broadcast."""
        text = msg.get("text", "")
        from_peer = client.peer_name or "unknown"
        sent_to = []

        # Reload config
        self.config = load_config()

        for peer_config in self.config.peers.values():
            if peer_config.name == from_peer:
                continue
            if not peer_config.tmux_session:
                continue
            pane = self._get_peer_pane(peer_config.tmux_session)
            if pane:
                formatted_message = f"@{from_peer} broadcasts: {text}"
                pane.send_keys(formatted_message, enter=True)
                sent_to.append(peer_config.name)

        await self._send(client, {"type": "ok", "sent_to": sent_to})

    async def _handle_hook_response(self, msg: dict[str, Any]) -> None:
        """Handle a response from a Stop hook."""
        correlation_id = msg.get("correlation_id")
        response = msg.get("response")

        if not correlation_id or not response:
            return

        for client in self._clients.values():
            future = client.pending_queries.get(correlation_id)
            if future and not future.done():
                future.set_result(response)
                return

    def _get_peers(self) -> list[Peer]:
        """Get list of all peers with status."""
        peers = []
        for peer_config in self.config.peers.values():
            if peer_config.tmux_session:
                status = self._get_peer_status(peer_config.tmux_session)
            else:
                status = PeerStatus.OFFLINE

            peers.append(Peer(
                name=peer_config.name,
                path=peer_config.path,
                machine=self.machine,
                tmux_session=peer_config.tmux_session,
                status=status,
                last_seen=datetime.utcnow() if status != PeerStatus.OFFLINE else None,
            ))
        return peers

    def _parse_tmux_target(self, tmux_target: str) -> tuple[str, str | None]:
        """Parse 'session:window' or 'session' format."""
        if ":" in tmux_target:
            session, window = tmux_target.split(":", 1)
            return session, window
        return tmux_target, None

    def _get_peer_status(self, tmux_target: str) -> PeerStatus:
        """Check if a peer's tmux session/window exists."""
        try:
            session_name, window_name = self._parse_tmux_target(tmux_target)
            session = self.tmux_server.sessions.get(session_name=session_name)
            if session is None:
                return PeerStatus.OFFLINE
            if window_name:
                window = session.windows.get(window_name=window_name)
                if window is None:
                    return PeerStatus.OFFLINE
            return PeerStatus.ONLINE
        except Exception:
            return PeerStatus.OFFLINE

    def _get_peer_pane(self, tmux_target: str) -> libtmux.Pane | None:
        """Get the active pane for a peer."""
        try:
            session_name, window_name = self._parse_tmux_target(tmux_target)
            session = self.tmux_server.sessions.get(session_name=session_name)
            if session is None:
                return None
            if window_name:
                window = session.windows.get(window_name=window_name)
                if window is None:
                    return None
                return window.active_pane
            return session.active_pane
        except Exception:
            return None


async def run_daemon(config: Config | None = None) -> None:
    """Run the daemon."""
    daemon = RepowireDaemon(config)
    await daemon.start()
    await daemon.run_forever()


def is_daemon_running() -> bool:
    """Check if daemon is already running."""
    if not PID_FILE.exists():
        return False

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return False


def get_daemon_pid() -> int | None:
    """Get the daemon PID if running."""
    if not PID_FILE.exists():
        return None

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        return None


if __name__ == "__main__":
    asyncio.run(run_daemon())
