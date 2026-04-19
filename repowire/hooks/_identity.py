"""Shared identity-path helpers for SessionStart + WebSocket hooks."""
import os


def resolve_agent_path(fallback_cwd: str | None = None) -> str:
    """Return identity path for daemon registration.

    Prefers REPOWIRE_AGENT_PATH (set by spawn-claude.sh to AGENTS_DIR) so that
    worker CWD = worktree (beads-6ay D7) does not change the derived display_name.

    Args:
        fallback_cwd: Fallback path when env var is unset. Typically
            input_data.get("cwd") or str(os.getcwd()). If None, uses os.getcwd().

    Returns:
        Path string to send as `path` in /peers registration and WS connect.
    """
    env_path = os.environ.get("REPOWIRE_AGENT_PATH")
    if env_path:
        return env_path
    if fallback_cwd is not None:
        return fallback_cwd
    return str(os.getcwd())
