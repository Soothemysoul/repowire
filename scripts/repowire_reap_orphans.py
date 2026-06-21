"""Reaper of orphan repowire websocket_hook / mcp processes (beads-n8pt).

Orphan := repowire-managed process whose tmux pane is dead AND whose peer is
not in the daemon's live registry. Conservative: any ambiguity -> keep alive.
NEVER targets `repowire serve` (daemon) or `graphify.serve`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepowireProc:
    pid: int
    kind: str          # "ws_hook" | "mcp"
    peer_id: str | None
    pane: str | None    # REPOWIRE_TMUX_PANE, e.g. "%328"


def find_orphans(
    procs: list[RepowireProc],
    live_panes: set[str],
    live_peer_ids: set[str],
) -> list[RepowireProc]:
    """Return procs that are orphan under the conservative AND-rule.

    A proc is orphan iff it has a peer_id (ours) AND its pane is dead
    (missing or not in live_panes) AND its peer is not live.
    """
    orphans = []
    for p in procs:
        if not p.peer_id:
            continue  # not a repowire-managed session proc -> skip
        pane_dead = p.pane is None or p.pane not in live_panes
        peer_dead = p.peer_id not in live_peer_ids
        if pane_dead and peer_dead:
            orphans.append(p)
    return orphans
