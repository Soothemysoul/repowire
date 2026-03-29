"""Normalize agent-specific hook payloads into a common format.

Each agent runtime (Claude Code, Codex, Gemini) sends different field names
for the same concepts. This adapter is the ONLY place that knows about
these differences. Handlers work with the normalized output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Event name normalization
_EVENT_MAP = {
    "AfterAgent": "Stop",
    "BeforeAgent": "UserPromptSubmit",
}

STOP_EVENTS = {"Stop", "AfterAgent"}
PROMPT_EVENTS = {"UserPromptSubmit", "BeforeAgent"}


@dataclass
class HookPayload:
    """Normalized hook payload, agent-agnostic."""

    event: str  # Canonical: "SessionStart", "Stop", "UserPromptSubmit"
    session_id: str
    cwd: str
    transcript_path: str | None
    response_text: str | None  # Agent's response (Stop hooks only)
    backend: str
    raw: dict  # Original payload for any agent-specific needs


def normalize(input_data: dict, backend: str) -> HookPayload:
    """Normalize an agent-specific hook payload into a common format."""
    raw_event = input_data.get("hook_event_name", "")
    event = _EVENT_MAP.get(raw_event, raw_event)

    # Response text: each agent uses a different field name
    response_text = (
        input_data.get("prompt_response")          # Gemini AfterAgent
        or input_data.get("last_assistant_message")  # Codex Stop
        or input_data.get("final_response")          # Future/generic
    )

    return HookPayload(
        event=event,
        session_id=input_data.get("session_id", ""),
        cwd=input_data.get("cwd", ""),
        transcript_path=input_data.get("transcript_path"),
        response_text=response_text,
        backend=backend,
        raw=input_data,
    )


def hook_output(backend: str) -> None:
    """Print required hook output to stdout. Gemini needs explicit approval."""
    if backend == "gemini":
        print(json.dumps({"decision": "allow"}))
