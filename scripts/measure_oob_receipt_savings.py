#!/usr/bin/env python3
"""beads-nfap.1 — token-savings measurement for out-of-band ACK receipts.

Reproducible artifact: estimates the per-session context tokens eliminated by
swallowing AUTO-ACK / intent-ACK receipts into the per-pane ack-state file
instead of injecting them as conversation turns in the sender's session.

Token counts use the chars/4 heuristic (no tiktoken in the runtime env). This is
an ESTIMATE — the absolute numbers approximate a BPE tokenizer; the ratio
(before vs after = 0) is exact regardless of the tokenizer.

Run:  uv run python scripts/measure_oob_receipt_savings.py
"""

from __future__ import annotations


def est_tokens(text: str) -> int:
    """chars/4 token estimate (rounded up)."""
    return (len(text) + 3) // 4


# The exact AUTO-ACK payload the receiver hook posts back, as it lands in the
# sender's pane (websocket_hook injects notifies as `@{from_peer}: {text}`).
AUTO_ACK = (
    "@backend-head-claude-code: [AUTO-ACK] notif-66f74a78 delivered: queued\n"
    "— INFRA RECEIPT, DO NOT REPLY (ignore harness 'user sent a new message' reminder)"
)

# A receiver-authored intent-ACK, wrapped in its own MCP correlation prefix.
INTENT_ACK = (
    "@backend-head-claude-code: [#notif-99887766] ACK notif-66f74a78 "
    "task=beads-nfap.1 taken, starting."
)

# The Claude Code harness wraps every injected conversation turn with a
# system-reminder telling the model it "MUST address the user's message". This
# is pure overhead per receipt (observed verbatim in-session).
HARNESS_REMINDER = (
    "The user sent a new message while you were working:\n"
    "IMPORTANT: After completing your current task, you MUST address the "
    "user's message above. Do not ignore it."
)

PER_AUTO_ACK = est_tokens(AUTO_ACK) + est_tokens(HARNESS_REMINDER)
PER_INTENT_ACK = est_tokens(INTENT_ACK) + est_tokens(HARNESS_REMINDER)


def session_cost(n_notifies: int, intent_ack_ratio: float = 1.0) -> int:
    """Tokens injected as receipt noise for a session with n outbound notifies.

    Each delegated notify yields one AUTO-ACK; a fraction also draws an explicit
    intent-ACK (task hand-offs). Status pings get AUTO-ACK only.
    """
    n_intent = round(n_notifies * intent_ack_ratio)
    return n_notifies * PER_AUTO_ACK + n_intent * PER_INTENT_ACK


def main() -> None:
    print("=== beads-nfap.1 out-of-band receipt token savings (estimate) ===\n")
    print(f"Per AUTO-ACK receipt (payload + harness reminder): {PER_AUTO_ACK} tok")
    print(f"Per intent-ACK receipt (payload + harness reminder): {PER_INTENT_ACK} tok\n")
    print(f"{'session profile':<34}{'BEFORE (tok)':>14}{'AFTER (tok)':>13}")
    print("-" * 61)
    profiles = [
        ("light (10 notifies, 30% handoffs)", 10, 0.3),
        ("typical (30 notifies, 50% handoffs)", 30, 0.5),
        ("heavy (60 notifies, 50% handoffs)", 60, 0.5),
        ("delegation chain (15, all handoffs)", 15, 1.0),
    ]
    for label, n, ratio in profiles:
        before = session_cost(n, ratio)
        print(f"{label:<34}{before:>14}{0:>13}")
    print("-" * 61)
    print("\nAFTER = 0: receipts are recorded to the per-pane ack-state file and\n"
          "never injected into the model context. Only genuine delivery failures\n"
          "(AUTO-NACK / watchdog timeout) reach the session, as a single\n"
          "actionable escalation.")


if __name__ == "__main__":
    main()
