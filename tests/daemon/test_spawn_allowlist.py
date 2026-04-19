"""Tests for spawn route prefix matcher."""
from repowire.daemon.routes.spawn import _command_allowed


def test_exact_match():
    assert _command_allowed("claude", ["claude", "spawn-claude.sh director"])


def test_prefix_with_space():
    assert _command_allowed(
        "spawn-claude.sh director --project=foo",
        ["spawn-claude.sh director"],
    )


def test_no_trailing_space_rejected():
    # "spawn-claude.sh directory" must NOT match "spawn-claude.sh director"
    assert not _command_allowed(
        "spawn-claude.sh directory",
        ["spawn-claude.sh director"],
    )


def test_empty_allowlist_rejects_everything():
    assert not _command_allowed("claude", [])
