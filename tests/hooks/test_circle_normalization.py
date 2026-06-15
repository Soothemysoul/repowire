"""Tests for repowire.hooks._tmux.normalize_circle.

Regression guard for the q2ok circle-misregistration outage: a pane in a
grouped/linked tmux session resolves ``#{session_name}`` to the *view* session
name (e.g. ``global-view-agents-brain-team``) instead of the base session
(``global``). When ``REPOWIRE_CIRCLE`` is unset, the ws-hook used to register
the peer into that view-circle. ``normalize_circle`` collapses a view-session
name back to its base circle.
"""

from repowire.hooks._tmux import normalize_circle


def test_global_view_collapses_to_global():
    assert normalize_circle("global-view-agents-brain-team") == "global"


def test_global_view_other_suffix_collapses_to_global():
    assert normalize_circle("global-view-zeon") == "global"


def test_plain_project_session_unchanged():
    assert normalize_circle("project-drafter") == "project-drafter"


def test_base_global_unchanged():
    assert normalize_circle("global") == "global"


def test_general_base_view_collapses_to_base():
    # The view-collapse is not special-cased to "global"; any
    # ``<base>-view-<suffix>`` returns ``<base>``.
    assert normalize_circle("project-drafter-view-tile2") == "project-drafter"


def test_none_passthrough():
    assert normalize_circle(None) is None


def test_empty_base_not_collapsed():
    # No real circle can have an empty base; leave malformed names untouched
    # so they surface rather than silently mapping to "".
    assert normalize_circle("-view-foo") == "-view-foo"


def test_empty_suffix_not_collapsed():
    assert normalize_circle("global-view-") == "global-view-"
