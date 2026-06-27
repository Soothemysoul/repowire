"""beads-rbox: display-only token economy helpers in repowire.naming.

These cover the presentation transform applied at the two display sites (pane +
telegram). The canonical wire-text is NOT touched here — see
tests/test_rbox_correlation_invariant.py for the merge-gate proving correlation
extraction is unaffected.
"""

from __future__ import annotations

from repowire.naming import display_peer_name, display_text


class TestDisplayPeerName:
    def test_pane_strips_suffix_for_orchestrator(self) -> None:
        # bypasses_circles role resolves bare → safe to strip in agent↔agent pane
        assert display_peer_name("director-claude-code", "orchestrator") == "director"

    def test_pane_strips_suffix_for_service(self) -> None:
        assert display_peer_name("telegram-claude-code", "service") == "telegram"

    def test_pane_strips_suffix_for_human(self) -> None:
        assert display_peer_name("dashboard-claude-code", "human") == "dashboard"

    def test_pane_keeps_full_name_for_agent(self) -> None:
        # Variant A / beads-7ijt.1: regular AGENT names do NOT resolve bare;
        # stripping in pane display would teach an un-addressable name (404).
        assert (
            display_peer_name("backend-head-claude-code", "agent")
            == "backend-head-claude-code"
        )

    def test_pane_keeps_full_name_when_role_unknown(self) -> None:
        assert (
            display_peer_name("backend-head-claude-code", None)
            == "backend-head-claude-code"
        )

    def test_telegram_strip_all_strips_agent_name(self) -> None:
        # User-facing: user never addresses peers via notify_peer, so stripping
        # any name is safe regardless of role.
        assert (
            display_peer_name("backend-head-claude-code", "agent", strip_all=True)
            == "backend-head"
        )

    def test_strip_all_ignores_role(self) -> None:
        assert (
            display_peer_name("worker-claude-code", None, strip_all=True) == "worker"
        )

    def test_strips_non_claude_backends_when_safe(self) -> None:
        assert display_peer_name("foo-codex", "service") == "foo"
        assert display_peer_name("bar-gemini", "service") == "bar"
        assert display_peer_name("baz-opencode", "service") == "baz"

    def test_name_without_backend_suffix_unchanged(self) -> None:
        # strip_backend_suffix returns None → keep the original name.
        assert display_peer_name("plain-name", "service") == "plain-name"
        assert display_peer_name("plain-name", "agent", strip_all=True) == "plain-name"


class TestDisplayText:
    def test_pane_drops_only_hash(self) -> None:
        # D2 2a: [#notif-XXX] -> [notif-XXX]; full notif id stays visible so the
        # receiver-LLM can still author `ACK notif-XXX`.
        assert (
            display_text("[#notif-deadbeef] hello world")
            == "[notif-deadbeef] hello world"
        )

    def test_telegram_drops_notif_marker(self) -> None:
        # D2 2c: [#notif-XXX] -> [XXX]; user never authors an intent-ACK.
        assert (
            display_text("[#notif-deadbeef] hello", drop_notif_marker=True)
            == "[deadbeef] hello"
        )

    def test_prefix_only_no_body(self) -> None:
        assert display_text("[#notif-deadbeef]") == "[notif-deadbeef]"
        assert display_text("[#notif-deadbeef]", drop_notif_marker=True) == "[deadbeef]"

    def test_text_without_prefix_unchanged(self) -> None:
        assert display_text("no prefix here") == "no prefix here"
        assert display_text("ACK notif-deadbeef task=x") == "ACK notif-deadbeef task=x"

    def test_prefix_not_at_start_unchanged(self) -> None:
        # Only a LEADING token is a display prefix; a notif id mid-text is left.
        assert (
            display_text("see [#notif-deadbeef] above")
            == "see [#notif-deadbeef] above"
        )

    def test_non_eight_hex_unchanged(self) -> None:
        # Canonical form is exactly 8 hex; anything else is not our display token.
        assert display_text("[#notif-xyz] hello") == "[#notif-xyz] hello"
