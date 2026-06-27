"""beads-rbox merge-gate (director's HARD invariant): the display transform must
NOT regress correlation extraction or the ACK-watchdog.

The canonical ``notif-XXXXXXXX`` stays in the wire ``text``; only the displayed
copy is shortened. These tests prove that every component which parses the
correlation id out of the wire text still resolves the FULL canonical id after a
shortened display form has been produced from the same source.
"""

from __future__ import annotations

from repowire.daemon.routes import messages as daemon_messages
from repowire.hooks import websocket_hook as wh
from repowire.naming import display_text
from repowire.telegram import bot as tg_bot

_WIRE = "[#notif-deadbeef] implement beads-x"
_CANONICAL = "notif-deadbeef"


def test_display_form_differs_from_wire() -> None:
    # The display copy is genuinely shortened (new behavior)...
    assert display_text(_WIRE) == "[notif-deadbeef] implement beads-x"
    assert display_text(_WIRE, drop_notif_marker=True) == "[deadbeef] implement beads-x"
    assert display_text(_WIRE) != _WIRE


def test_wire_text_untouched_resolves_in_websocket_hook() -> None:
    # ...while the canonical wire text still resolves the full id everywhere.
    assert wh._parse_correlation_id(_WIRE) == _CANONICAL
    m = wh._NOTIF_ID_IN_TEXT_RE.match(_WIRE[:64])
    assert m is not None and m.group(1) == _CANONICAL


def test_intent_ack_regex_unaffected() -> None:
    # The receiver-LLM still authors `ACK notif-XXX` against the full id, which
    # the shortened pane form ([notif-XXX]) preserves verbatim.
    ack = "ACK notif-deadbeef task=beads-x taken"
    m = wh._INTENT_ACK_RE.match(ack)
    assert m is not None and m.group(1) == _CANONICAL


def test_interrupt_ledger_regex_unaffected() -> None:
    m = daemon_messages._INTERRUPT_CORRELATION_RE.match(_WIRE[:64])
    assert m is not None and m.group(1) == _CANONICAL


def test_telegram_notif_extraction_unaffected() -> None:
    m = tg_bot._NOTIF_ID_RE.search(_WIRE)
    assert m is not None and m.group(1) == _CANONICAL
