"""Message routing logic.

Routes messages via WebSocket transport.
"""

import asyncio
import logging
from typing import Any

from repowire.config.models import DEFAULT_QUERY_TIMEOUT
from repowire.daemon.query_tracker import QueryTracker
from repowire.daemon.websocket_transport import TransportError, WebSocketTransport

logger = logging.getLogger(__name__)


class MessageRouter:
    """Routes messages via WebSocket."""

    def __init__(
        self,
        transport: WebSocketTransport,
        query_tracker: QueryTracker,
    ):
        self._transport = transport
        self._query_tracker = query_tracker

    async def send_query(
        self,
        from_peer: str,
        to_session_id: str,
        to_peer_name: str,
        text: str,
        timeout: float = DEFAULT_QUERY_TIMEOUT,
        from_peer_id: str | None = None,
    ) -> str:
        """Send query and wait for response.

        Args:
            from_peer: Display name of sender
            to_session_id: Session ID of recipient
            to_peer_name: Display name of recipient (for logging)
            text: Query text
            timeout: Timeout in seconds
            from_peer_id: Authenticated peer_id of the sender (beads-hqvm),
                threaded to the receiver so its AUTO-ACK can reply to the exact
                original sender by peer_id (avoids cross-circle ACK misrouting).

        Returns:
            Response text

        Raises:
            ValueError: If peer not connected
            TimeoutError: If no response within timeout
            TransportError: If send fails
        """
        if not self._transport.is_connected(to_session_id):
            raise ValueError(f"Peer {to_peer_name} not connected")

        # Register query
        correlation_id = await self._query_tracker.register_query(
            from_peer=from_peer,
            to_peer_id=to_session_id,
            to_peer_name=to_peer_name,
            query_text=text,
        )

        future = self._query_tracker.get_future(correlation_id)
        if not future:
            raise ValueError("Query tracking error")

        # Send via WebSocket
        message: dict[str, Any] = {
            "type": "query",
            "correlation_id": correlation_id,
            "from_peer": from_peer,
            "text": text,
        }
        if from_peer_id is not None:
            message["from_peer_id"] = from_peer_id

        try:
            await self._transport.send(to_session_id, message)
            logger.info(f"Query sent: {from_peer} -> {to_peer_name} ({correlation_id[:8]})")

            # Wait for response
            response = await asyncio.wait_for(future, timeout=timeout)
            logger.info(f"Query resolved: {from_peer} -> {to_peer_name} ({correlation_id[:8]})")
            return response

        except asyncio.TimeoutError:
            logger.warning(f"Query timeout: {from_peer} -> {to_peer_name} ({correlation_id[:8]})")
            raise TimeoutError(f"No response from {to_peer_name} within {timeout}s")

        except TransportError as e:
            logger.error(f"Transport error: {e}")
            raise

        finally:
            await self._query_tracker.cleanup_query(correlation_id)

    async def send_notification(
        self,
        from_peer: str,
        to_session_id: str,
        to_peer_name: str,
        text: str,
        interrupt: bool = False,
        from_peer_role: str | None = None,
        from_peer_id: str | None = None,
    ) -> None:
        """Send notification (fire-and-forget).

        Args:
            from_peer: Display name of sender
            to_session_id: Session ID of recipient
            to_peer_name: Display name of recipient (for logging)
            text: Notification text
            interrupt: If True, receiver hook re-adds Escape before paste so
                the message cancels the receiver's current turn (beads-61w).
                Default False — message queues naturally in tty buffer.
            from_peer_role: Sender peer role (agent/service/orchestrator/…).
                Used receiver-side to skip auto-ACK back to service peers
                (telegram, brain-admin) that have no turn-concept.
            from_peer_id: Authenticated peer_id of the sender (beads-hqvm).
                Threaded to the receiver so its AUTO-ACK can reply to the exact
                original sender by peer_id, avoiding the display_name ambiguity
                that misroutes ACKs across circles.

        Raises:
            TransportError: If send fails
        """
        message: dict[str, Any] = {
            "type": "notify",
            "from_peer": from_peer,
            "text": text,
            "interrupt": interrupt,
        }
        if from_peer_role is not None:
            message["from_peer_role"] = from_peer_role
        if from_peer_id is not None:
            message["from_peer_id"] = from_peer_id

        await self._transport.send(to_session_id, message)
        logger.info(f"Notification sent: {from_peer} -> {to_peer_name}")

    async def broadcast(
        self,
        from_peer: str,
        text: str,
        exclude: set[str] | None = None,
        from_peer_id: str | None = None,
    ) -> list[str]:
        """Broadcast to all connected peers.

        Args:
            from_peer: Display name of sender
            text: Broadcast text
            exclude: Set of session IDs to exclude
            from_peer_id: Authenticated peer_id of the sender (beads-fqus).
                Threaded into the WS frame — exactly like send_notification —
                so each receiver's AUTO-ACK can reply to the exact original
                sender by peer_id instead of misrouting by ambiguous
                display_name.

        Returns:
            List of session IDs that received the broadcast
        """
        excluded = exclude or set()
        message: dict[str, Any] = {
            "type": "broadcast",
            "from_peer": from_peer,
            "text": text,
        }
        if from_peer_id is not None:
            message["from_peer_id"] = from_peer_id

        async def _send_one(session_id: str) -> str | None:
            try:
                await self._transport.send(session_id, message)
                return session_id
            except TransportError as e:
                logger.warning(f"Broadcast to {session_id} failed: {e}")
                return None

        results = await asyncio.gather(
            *(_send_one(sid) for sid in self._transport.get_all_sessions() if sid not in excluded),
        )
        sent_to = [r for r in results if r is not None]

        logger.info(f"Broadcast from {from_peer}: sent to {len(sent_to)} peers")
        return sent_to

    async def broadcast_refresh(
        self,
        *,
        target_epoch: str,
        reason: str,
        scope: str,
        exclude: set[str] | None = None,
    ) -> list[str]:
        """Push a client-refresh control frame to every live session (beads-rz1g).

        Unlike :meth:`broadcast`, this is a daemon-originated control message
        (``type=="refresh"``) delivered to ALL connected sessions regardless of
        circle — a deploy-time signal, not peer-to-peer chat. The receiving
        ws-hook writes a ``.refresh-pending`` marker (never restarts mid-turn);
        the stop-hook performs the actual self-restart at a safe turn boundary.

        Args:
            target_epoch: the deployed epoch sessions should converge to. A
                session whose loaded epoch differs is stale and refreshes.
            reason: human-readable trigger (echoed into the marker, surfaced in
                the resumption context).
            scope: ``workers`` | ``all`` | ``advisory`` — the client decides what
                to do with it based on its own role (orchestrators are always
                advisory; see stop-hook).
            exclude: session IDs to skip.

        Returns:
            List of session IDs the refresh frame reached.
        """
        excluded = exclude or set()
        message: dict[str, Any] = {
            "type": "refresh",
            "target_epoch": target_epoch,
            "reason": reason,
            "scope": scope,
        }

        async def _send_one(session_id: str) -> str | None:
            try:
                await self._transport.send(session_id, message)
                return session_id
            except TransportError as e:
                logger.warning(f"Refresh to {session_id} failed: {e}")
                return None

        results = await asyncio.gather(
            *(_send_one(sid) for sid in self._transport.get_all_sessions() if sid not in excluded),
        )
        sent_to = [r for r in results if r is not None]
        logger.info(
            f"Refresh broadcast (epoch={target_epoch}, scope={scope}): "
            f"sent to {len(sent_to)} sessions"
        )
        return sent_to
