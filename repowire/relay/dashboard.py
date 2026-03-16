"""Datastar-powered dashboard routes for the relay server.

Serves the dashboard HTML and SSE endpoints that push live updates
as HTML fragments using Datastar's patch-elements protocol.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader

log = logging.getLogger(__name__)

# Jinja2 environment
_templates_dir = Path(__file__).parent / "templates"
_jinja = Environment(loader=FileSystemLoader(str(_templates_dir)), autoescape=False)


def _peer_label(peer: dict) -> str:
    """Human-readable label: folder name > session ID."""
    if peer.get("path"):
        folder = peer["path"].rstrip("/").split("/")[-1]
        if folder:
            return folder
    return peer.get("display_name", peer.get("name", "?"))


def _short_path(path: str) -> tuple[str, str]:
    """Returns (parent, folder) for display."""
    parts = path.rstrip("/").split("/")
    folder = parts[-1] if parts else path
    parent = f"…/{parts[-2]}/" if len(parts) > 2 else f"{parts[-2]}/" if len(parts) == 2 else ""
    return parent, folder


def _enrich_peer(peer: dict) -> dict:
    """Add display helpers to a peer dict."""
    peer = dict(peer)
    peer["label"] = _peer_label(peer)
    parent, folder = _short_path(peer.get("path", ""))
    peer["parent_path"] = parent
    peer["folder"] = folder
    return peer


def _format_event(event: dict) -> dict:
    """Add display helpers to an event dict."""
    event = dict(event)
    ts = event.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(ts)
        event["time"] = dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        event["time"] = ""

    if event.get("type") == "chat_turn" and event.get("role") == "assistant":
        event["html"] = markdown.markdown(
            event.get("text", ""),
            extensions=["fenced_code", "tables"],
        )
    return event


def _render_sidebar(peers: list[dict]) -> str:
    """Render sidebar partial."""
    active = [_enrich_peer(p) for p in peers if p.get("status") in ("online", "busy")]
    active.sort(key=lambda p: (0 if p["status"] == "online" else 1, p["label"]))
    offline_count = sum(1 for p in peers if p.get("status") == "offline")
    tmpl = _jinja.get_template("partials/sidebar.html")
    return tmpl.render(active_peers=active, offline_count=offline_count)


def _render_overview(peers: list[dict], events: list[dict]) -> str:
    """Render overview partial."""
    active = [_enrich_peer(p) for p in peers if p.get("status") in ("online", "busy")]
    active.sort(key=lambda p: (0 if p["status"] == "online" else 1, p["label"]))
    online = sum(1 for p in active if p["status"] == "online")
    busy = sum(1 for p in active if p["status"] == "busy")

    recent = []
    for e in reversed(events[-10:]):
        fe = _format_event(e)
        if e.get("type") in ("notification", "query", "broadcast"):
            recent.append(fe)

    tmpl = _jinja.get_template("partials/overview.html")
    return tmpl.render(
        active_peers=active,
        online_count=online,
        busy_count=busy,
        event_count=len(events),
        recent_events=recent,
    )


def _render_peer_detail(peer: dict, events: list[dict]) -> str:
    """Render peer detail partial."""
    peer = _enrich_peer(peer)
    project_name = peer.get("path", "").rstrip("/").split("/")[-1] if peer.get("path") else ""

    messages = []
    for e in events:
        if e.get("type") != "chat_turn":
            continue
        if not e.get("text", "").strip():
            continue  # skip empty chat turns
        ep = e.get("peer", "")
        if ep == peer["name"] or ep == peer.get("display_name") or ep == project_name:
            fe = _format_event(e)
            messages.append(fe)

    # Also include notifications to/from this peer
    for e in events:
        if e.get("type") in ("notification", "query"):
            if e.get("from") == peer["name"] or e.get("to") == peer["name"]:
                fe = _format_event(e)
                fe["role"] = "trace"
                text = e.get("text", "")[:80]
                fe["text"] = f"⇢ {e['type']} {e.get('from','?')} → {e.get('to','?')}: {text}"
                messages.append(fe)

    messages.sort(key=lambda m: m.get("timestamp", ""))

    tmpl = _jinja.get_template("partials/peer_detail.html")
    return tmpl.render(peer=peer, messages=messages)


def render_dashboard_html(
    sidebar_html: str = "",
    content_html: str = "",
    online_count: int = 0,
) -> str:
    """Render the full dashboard HTML page with optional pre-rendered content."""
    return _jinja.get_template("dashboard.html").render(
        sidebar_html=sidebar_html,
        content_html=content_html,
        online_count=online_count,
    )


async def generate_sse_updates(
    get_peers_fn,
    get_events_fn,
    user_id: str,
):
    """Generator that yields Datastar SSE events for live dashboard updates.

    Args:
        get_peers_fn: async callable returning peers list
        get_events_fn: async callable returning events list
        user_id: authenticated user ID
    """
    import asyncio

    from datastar_py import ServerSentEventGenerator as SseGen  # noqa: N814

    last_hash = None
    heartbeat_counter = 0

    while True:
        try:
            peers = await get_peers_fn()
            events = await get_events_fn()

            # Detect changes via content hash
            content_hash = hash((
                tuple((p.get("name"), p.get("status"), p.get("description")) for p in peers),
                len(events),
                events[-1].get("id") if events else None,
            ))

            if content_hash != last_hash:
                last_hash = content_hash

                sidebar_html = _render_sidebar(peers)
                overview_html = _render_overview(peers, events)
                online_count = sum(
                    1 for p in peers if p.get("status") in ("online", "busy")
                )

                yield SseGen.patch_elements(
                    f'<div id="sidebar">{sidebar_html}</div>',
                    selector="#sidebar",
                    mode="inner",
                )
                yield SseGen.patch_elements(
                    f'<div id="mobile-sidebar">{sidebar_html}</div>',
                    selector="#mobile-sidebar",
                    mode="inner",
                )
                yield SseGen.patch_elements(
                    overview_html,
                    selector="#main-content",
                    mode="inner",
                )
                yield SseGen.patch_elements(
                    f'<span id="online-count" class="tabular-nums">'
                    f'{online_count} online</span>',
                    selector="#online-count",
                )
                heartbeat_counter = 0
            else:
                heartbeat_counter += 1
                # Send keepalive every ~15s (heartbeat_counter * 2s sleep)
                if heartbeat_counter >= 7:
                    heartbeat_counter = 0
                    # Datastar ignores comments, but they keep Cloudflare alive
                    yield ": keepalive\n\n"

        except Exception:
            log.debug("SSE update error", exc_info=True)

        await asyncio.sleep(2)
