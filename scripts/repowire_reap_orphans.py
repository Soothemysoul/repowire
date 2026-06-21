"""Reaper of orphan repowire websocket_hook / mcp processes (beads-n8pt).

Orphan := repowire-managed process whose tmux pane is dead AND whose peer is
not in the daemon's live registry. Conservative: any ambiguity -> keep alive.
NEVER targets `repowire serve` (daemon) or `graphify.serve`.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx

DAEMON_URL = os.environ.get("REPOWIRE_DAEMON_URL", "http://127.0.0.1:8377")
TMUX_SOCKET = os.environ.get("REPOWIRE_TMUX_SOCKET", "workspace")


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


def classify_cmdline(cmdline: str) -> str | None:
    if "repowire/hooks/websocket_hook.py" in cmdline:
        return "ws_hook"
    if cmdline.rstrip().endswith("repowire mcp") or "/repowire mcp" in cmdline:
        return "mcp"
    return None  # daemon (`repowire serve`), graphify, anything else -> skip


def parse_environ(raw: str) -> tuple[str | None, str | None]:
    env = {}
    for item in raw.split("\x00"):
        if "=" in item:
            k, _, v = item.partition("=")
            env[k] = v
    return env.get("REPOWIRE_PEER_ID"), env.get("REPOWIRE_TMUX_PANE")


def gather_procs() -> list[RepowireProc]:
    out = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=True
    ).stdout
    procs: list[RepowireProc] = []
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_str, _, cmdline = line.partition(" ")
        kind = classify_cmdline(cmdline)
        if kind is None:
            continue
        try:
            pid = int(pid_str)
            with open(f"/proc/{pid}/environ") as f:
                raw = f.read()
        except (ValueError, OSError):
            continue  # gone / no perm -> skip
        peer, pane = parse_environ(raw)
        procs.append(RepowireProc(pid=pid, kind=kind, peer_id=peer, pane=pane))
    return procs


def gather_live_panes() -> set[str]:
    out = subprocess.run(
        ["tmux", "-L", TMUX_SOCKET, "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return {p.strip() for p in out.splitlines() if p.strip()}


def live_peer_ids(peers: list[dict]) -> set[str]:
    """Live = not offline (online AND busy). busy = active session mid-task,
    must stay guarded; online-only would collapse the AND-rule to pane-only.
    """
    return {p["peer_id"] for p in peers if p.get("status") != "offline"}


def gather_live_peer_ids() -> set[str]:
    token = os.environ.get("REPOWIRE_AUTH_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.get(f"{DAEMON_URL}/peers", headers=headers, timeout=10.0)
    resp.raise_for_status()
    return live_peer_ids(resp.json()["peers"])


def reap(orphans: list[RepowireProc], apply: bool) -> None:
    for o in orphans:
        print(f"[orphan] pid={o.pid} kind={o.kind} peer={o.peer_id} pane={o.pane}")
        if not apply:
            continue
        try:
            os.kill(o.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    if apply and orphans:
        time.sleep(3)
        for o in orphans:
            try:
                os.kill(o.pid, 0)
                os.kill(o.pid, signal.SIGKILL)  # still alive -> hard kill
            except ProcessLookupError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reap orphan repowire ws_hook/mcp procs")
    ap.add_argument("--apply", action="store_true", help="actually kill (default: dry-run)")
    args = ap.parse_args(argv)
    procs = gather_procs()
    orphans = find_orphans(procs, gather_live_panes(), gather_live_peer_ids())
    if not orphans:
        print("no orphans found")
        return 0
    reap(orphans, apply=args.apply)
    print(f"{'killed' if args.apply else 'would kill'} {len(orphans)} orphan(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
