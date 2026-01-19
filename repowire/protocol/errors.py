"""Custom error types for Repowire protocol."""


class PeerDisconnectedError(Exception):
    """Raised when a peer disconnects during a pending query."""

    def __init__(self, peer_name: str):
        self.peer_name = peer_name
        super().__init__(f"Peer '{peer_name}' disconnected")
