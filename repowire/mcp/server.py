"""MCP server - thin client that delegates to daemon."""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from repowire.daemon.client import DaemonClient

_client: DaemonClient | None = None
_my_peer_name: str | None = None


def _detect_my_peer_name() -> str:
    """Detect current peer name from cwd folder name (matches session_handler)."""
    global _my_peer_name
    if _my_peer_name:
        return _my_peer_name

    # Use folder name as peer name (same as session_handler)
    _my_peer_name = Path.cwd().name
    return _my_peer_name


async def get_client() -> DaemonClient:
    """Get the daemon client, creating and connecting if needed."""
    global _client
    if _client is None:
        _client = DaemonClient(peer_name=_detect_my_peer_name())

    if not _client._connected:
        if not await _client.connect(auto_start=True):
            raise RuntimeError("Cannot connect to repowire daemon")

    return _client


def create_mcp_server() -> FastMCP:
    """Create the MCP server."""
    mcp = FastMCP("repowire")

    @mcp.tool()
    async def list_peers() -> list[dict]:
        """List all registered peers in the mesh.

        Returns a list of peers with their name, path, machine, and status.
        """
        client = await get_client()
        return await client.list_peers()

    @mcp.tool()
    async def ask_peer(peer_name: str, query: str) -> str:
        """Ask a peer a question and wait for their response.

        Args:
            peer_name: Name of the peer to ask (e.g., "backend", "frontend")
            query: The question or request to send

        Returns:
            The peer's response text
        """
        client = await get_client()
        return await client.query(peer_name, query)

    @mcp.tool()
    async def notify_peer(peer_name: str, message: str) -> str:
        """Send a notification to a peer (fire-and-forget).

        Use this ONLY when you need to proactively share information with another
        peer without expecting a response. Examples:
        - Announcing completion of a task that affects other peers
        - Sharing a status update or warning
        - Informing about changes to shared resources

        Do NOT use notify_peer to respond to ask_peer queries - your response
        is automatically captured and returned to the caller.

        Args:
            peer_name: Name of the peer to notify
            message: The notification message

        Returns:
            Confirmation message
        """
        client = await get_client()
        await client.notify(peer_name, message)
        return f"Notification sent to {peer_name}"

    @mcp.tool()
    async def broadcast(message: str) -> str:
        """Send a message to all online peers.

        Use for announcements that affect everyone, like deployment updates
        or breaking changes. Do NOT use for responses to queries.

        Args:
            message: The message to broadcast

        Returns:
            Confirmation message
        """
        client = await get_client()
        sent_to = await client.broadcast(message)
        return f"Broadcast sent to: {', '.join(sent_to) if sent_to else 'no peers online'}"

    return mcp


async def run_mcp_server() -> None:
    """Run the MCP server."""
    mcp = create_mcp_server()
    try:
        await mcp.run_stdio_async()
    finally:
        if _client is not None:
            await _client.disconnect()
