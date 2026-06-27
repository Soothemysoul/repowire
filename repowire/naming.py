"""Shared display-name helpers used by both spawn and peer_registry."""

from __future__ import annotations

import re
from pathlib import Path

from repowire.config.models import AgentType
from repowire.protocol.peers import PeerRole

# beads-rbox D1 (1a): a bare (suffix-stripped) name is addressable ONLY for peers
# whose role auto-bypasses circles — the daemon's stem-alias fallback
# (``_alias_resolve_unlocked``, beads-7ijt.1 Variant A) resolves a bare name only
# for these roles. Mirrors ``Peer.bypasses_circles``; keep the two in sync. To
# extend display-stripping to regular agents (1b), widen this set ONLY after the
# daemon's bare-resolution is widened to match — otherwise the stripped name
# 404s when an LLM copies it into ``notify_peer``.
_BARE_RESOLVABLE_ROLES = frozenset(
    r.value for r in (PeerRole.SERVICE, PeerRole.ORCHESTRATOR, PeerRole.HUMAN)
)

# A leading display token: ``[#notif-XXXXXXXX]`` (exactly 8 hex), at string start.
_NOTIF_DISPLAY_RE = re.compile(r"^\[#(notif-[a-f0-9]{8})\]")

# A pane attached to a grouped/linked tmux session (e.g. Tilix two-pane UI)
# resolves ``#{session_name}`` to the *view* session, conventionally named
# ``<base>-view-<suffix>`` (observed: ``global-view-agents-brain-team``). The
# circle is the *base* session, never the view alias.
_VIEW_SESSION_MARKER = "-view-"


def normalize_circle(session_name: str | None) -> str | None:
    """Collapse a grouped-session *view* name to its base circle.

    ``<base>-view-<suffix>`` -> ``<base>`` (e.g.
    ``global-view-agents-brain-team`` -> ``global``). Names without the view
    marker are returned unchanged, as are malformed names with an empty base
    or suffix (so they surface rather than silently mapping to "").

    Shared by the ws-hook circle fallback (registration-time client side) and
    the daemon's ``allocate_and_register`` guard (defense-in-depth, server
    side), so both layers agree on what a view-circle collapses to.
    """
    if not session_name:
        return session_name
    base, marker, suffix = session_name.partition(_VIEW_SESSION_MARKER)
    if marker and base and suffix:
        return base
    return session_name


def sanitize_folder_name(name: str) -> str:
    """Sanitize an arbitrary folder name for use in a peer display_name.

    Replaces characters not matching [a-zA-Z0-9._-] with hyphens,
    collapses runs, strips leading/trailing hyphens.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "-", name)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    sanitized = sanitized.strip("-")
    return sanitized or "peer"


def build_base_display_name(path: str | None, backend: AgentType | str) -> str:
    """Return the canonical base display name for a peer (without collision suffix).

    Format: ``{sanitized-folder}-{backend.value}``
    This is what the daemon's ``_build_display_name`` assigns when no collision
    exists, and what ``spawn_peer`` must predict so that the spawn-ready waiter
    key matches the WebSocket fire-event key.
    """
    folder = sanitize_folder_name(Path(path).name) if path else "peer"
    backend_str = backend.value if isinstance(backend, AgentType) else str(backend)
    return f"{folder}-{backend_str}"


def strip_backend_suffix(display_name: str) -> str | None:
    """Inverse of the ``-{backend}`` suffix that ``build_base_display_name`` adds.

    Strips a trailing ``-<backend.value>`` for any known ``AgentType``, returning
    the bare stem (the role/folder part, e.g. ``telegram-claude-code`` ->
    ``telegram``). Returns ``None`` when the name carries no recognized backend
    suffix (or would strip to empty) — callers use that to bail out of
    stem-aliasing rather than alias a name they cannot decompose.

    Kept next to ``build_base_display_name`` so the build/strip pair stays a
    single source of truth: the backend-suffix set must not drift between them.
    """
    for backend in AgentType:
        suffix = f"-{backend.value}"
        if display_name.endswith(suffix) and len(display_name) > len(suffix):
            return display_name[: -len(suffix)]
    return None


def _role_resolves_bare(role: PeerRole | str | None) -> bool:
    """Whether a peer with ``role`` resolves when addressed by its bare stem.

    Mirrors ``Peer.bypasses_circles`` (beads-7ijt.1 Variant A): only SERVICE /
    ORCHESTRATOR / HUMAN are stem-aliased by the daemon. ``role`` may be a
    ``PeerRole`` or its string value (the WS ``from_peer_role`` arrives as a str).
    """
    if role is None:
        return False
    value = role.value if isinstance(role, PeerRole) else str(role)
    return value in _BARE_RESOLVABLE_ROLES


def display_peer_name(
    display_name: str,
    role: PeerRole | str | None = None,
    *,
    strip_all: bool = False,
) -> str:
    """Return the display form of a peer name (beads-rbox D1).

    Strips the ``-<backend>`` suffix when doing so is safe:

    - ``strip_all=True`` (user-facing display, e.g. telegram): always strip —
      the user never addresses a peer via ``notify_peer``, so an un-addressable
      stem is harmless.
    - otherwise (agent↔agent pane): strip ONLY when ``role`` resolves bare, so
      the displayed name stays addressable. Regular AGENT names keep their full
      ``-<backend>`` suffix (Variant A) to avoid the 404 footgun where an LLM
      copies the displayed name back into ``notify_peer``.

    Names with no recognized backend suffix are returned unchanged.
    """
    if not (strip_all or _role_resolves_bare(role)):
        return display_name
    return strip_backend_suffix(display_name) or display_name


def display_text(text: str, *, drop_notif_marker: bool = False) -> str:
    """Shorten a leading ``[#notif-XXXXXXXX]`` display token (beads-rbox D2).

    Presentation-only — the caller must pass a copy used for display, never the
    canonical wire ``text`` that correlation/ACK/interrupt-ledger logic parses.

    - default (pane, D2 2a): ``[#notif-XXX]`` -> ``[notif-XXX]`` (drop only the
      ``#``). The full ``notif-XXX`` stays visible so the receiver-LLM can still
      author ``ACK notif-XXX``.
    - ``drop_notif_marker=True`` (telegram, D2 2c): ``[#notif-XXX]`` -> ``[XXX]``
      (drop ``#notif-``). The user never authors an intent-ACK.

    Text whose start is not exactly ``[#notif-<8 hex>]`` is returned unchanged.
    """
    m = _NOTIF_DISPLAY_RE.match(text)
    if not m:
        return text
    notif_id = m.group(1)  # "notif-XXXXXXXX"
    inner = notif_id[len("notif-") :] if drop_notif_marker else notif_id
    return f"[{inner}]{text[m.end():]}"
