"""Client for connecting to the repowire daemon."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SOCKET_PATH = Path("/tmp/repowire.sock")


class DaemonClient:
    """Client for communicating with the repowire daemon."""

    def __init__(self, peer_name: str | None = None) -> None:
        self.peer_name = peer_name
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._client_id: str | None = None

    async def connect(self, auto_start: bool = True) -> bool:
        """Connect to the daemon, optionally starting it if not running."""
        if self._connected:
            return True

        if not SOCKET_PATH.exists():
            if auto_start:
                if not await self._start_daemon():
                    return False
            else:
                return False

        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                str(SOCKET_PATH)
            )
            self._connected = True

            if self.peer_name:
                await self._send({"type": "register", "peer_name": self.peer_name})
                response = await self._recv()
                self._client_id = response.get("client_id")

            return True
        except Exception:
            return False

    async def disconnect(self) -> None:
        """Disconnect from the daemon."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self._connected = False

    async def _send(self, message: dict[str, Any]) -> None:
        """Send a message to the daemon."""
        if not self._writer:
            raise RuntimeError("Not connected")
        data = json.dumps(message).encode() + b"\n"
        self._writer.write(data)
        await self._writer.drain()

    async def _recv(self) -> dict[str, Any]:
        """Receive a message from the daemon."""
        if not self._reader:
            raise RuntimeError("Not connected")
        data = await self._reader.readline()
        if not data:
            raise ConnectionError("Daemon closed connection")
        return json.loads(data.decode())

    async def _start_daemon(self) -> bool:
        """Start the daemon in the background."""
        try:
            project_dir = Path(__file__).parent.parent.parent

            if (project_dir / "pyproject.toml").exists():
                cmd = [
                    sys.executable, "-m", "repowire.daemon.server"
                ]
            else:
                cmd = ["repowire", "daemon", "start"]

            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(project_dir),
            )

            for _ in range(50):
                await asyncio.sleep(0.1)
                if SOCKET_PATH.exists():
                    return True

            return False
        except Exception:
            return False

    async def _request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a request and get response, with reconnection on failure."""
        for attempt in range(2):
            if not self._connected:
                if not await self.connect():
                    raise RuntimeError("Cannot connect to daemon")
            try:
                await self._send(message)
                return await self._recv()
            except (BrokenPipeError, ConnectionError, OSError):
                self._connected = False
                if attempt == 0:
                    continue
                raise

        raise RuntimeError("Failed to communicate with daemon")

    async def list_peers(self) -> list[dict[str, Any]]:
        """List all peers."""
        response = await self._request({"type": "list_peers"})
        return response.get("peers", [])

    async def query(
        self,
        to_peer: str,
        text: str,
        timeout: float = 120.0,
    ) -> str:
        """Send a query and wait for response."""
        response = await self._request({
            "type": "query",
            "to_peer": to_peer,
            "text": text,
            "timeout": timeout,
        })

        if "error" in response:
            raise ValueError(response["error"])

        return response.get("text", "")

    async def notify(self, to_peer: str, text: str) -> None:
        """Send a notification (fire-and-forget)."""
        response = await self._request({
            "type": "notify",
            "to_peer": to_peer,
            "text": text,
        })
        if "error" in response:
            raise ValueError(response["error"])

    async def broadcast(self, text: str) -> list[str]:
        """Broadcast to all peers."""
        response = await self._request({
            "type": "broadcast",
            "text": text,
        })
        return response.get("sent_to", [])
