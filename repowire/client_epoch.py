"""Client-code epoch — a deterministic version+build marker (beads-rz1g).

The per-session MCP client and ws-hook load the installed ``repowire`` package
into memory once, at spawn. A ``uv tool install`` reinstall + daemon restart
updates only the daemon; long-lived sessions keep executing the OLD in-memory
code until they respawn. The epoch lets us *detect* that staleness:

- the daemon computes its epoch at startup → the authoritative *deployed* epoch,
  echoed on the WS handshake and pushed via ``POST /control/refresh-clients``;
- each session captures its *loaded* epoch at hook startup (cached, NOT
  recomputed from disk later — a reinstall must not retroactively mutate a
  running process's loaded epoch) and writes it into its pane-meta.

A mismatch means the session predates the last reinstall and should refresh
(self-restart at a safe turn boundary). The value is therefore only meaningful
when captured at each process's OWN startup; callers cache it.
"""

from __future__ import annotations

import os
from pathlib import Path


def compute_client_epoch() -> str:
    """Return ``"<version>+<mtime_ns>"`` identifying the installed client build.

    Combines the installed package version with the mtime (in ns) of the package
    ``__init__.py``, so a reinstall that rewrites the files yields a new epoch
    even when the version string is unchanged. Deterministic for a given on-disk
    install; cheap (one stat, no hashing). Degrades to ``"<version>+0"`` if the
    package file cannot be stat'd.
    """
    try:
        from repowire import __version__ as version
    except Exception:
        version = "unknown"
    try:
        import repowire

        init_path = Path(repowire.__file__).resolve()
        mtime_ns = os.stat(init_path).st_mtime_ns
    except (OSError, AttributeError, TypeError):
        mtime_ns = 0
    return f"{version}+{mtime_ns}"
