"""POST /control/refresh-clients helper for the deploy script (beads-n8pt).

Contract (frozen, beads-rz1g): POST {reason, scope[, target_epoch]} -> 200.
target_epoch omitted by default — daemon derives its own deployed-epoch
post-restart (CONFIRMED by backend-head, notif-d800fdec).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

_VALID_SCOPES = {"workers", "all", "advisory"}


def build_request(daemon_url: str, reason: str, scope: str, token: str | None):
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {_VALID_SCOPES}, got {scope!r}")
    url = f"{daemon_url.rstrip('/')}/control/refresh-clients"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"reason": reason, "scope": scope}
    return "POST", url, headers, body


def describe_response(text: str) -> str:
    """Summarize the endpoint reply for deploy logs.

    Contract reply (rz1g): {notified: int, target_epoch: str}. Render the
    notified-session count and epoch when present; fall back to the raw body
    (truncated) for a leaner/older daemon so logging never masks a real reply.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text[:200]
    if not isinstance(data, dict):
        return text[:200]
    notified = data.get("notified")
    epoch = data.get("target_epoch")
    if notified is None and epoch is None:
        return text[:200]
    return f"notified={notified} target_epoch={epoch}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--daemon-url",
        default=os.environ.get("REPOWIRE_DAEMON_URL", "http://127.0.0.1:8377"),
    )
    ap.add_argument("--reason", required=True)
    ap.add_argument("--scope", default="workers")
    # Token from env $REPOWIRE_AUTH_TOKEN by default (backend-head notif-d800fdec).
    ap.add_argument("--token", default=os.environ.get("REPOWIRE_AUTH_TOKEN") or None)
    args = ap.parse_args(argv)
    method, url, headers, body = build_request(
        args.daemon_url, args.reason, args.scope, args.token
    )
    resp = httpx.request(method, url, headers=headers, json=body, timeout=30.0)
    resp.raise_for_status()  # non-200 -> deploy fails loudly
    # Contract reply (rz1g): {notified: int, target_epoch: str}. Log the count
    # of reached sessions for operational visibility; tolerate a leaner body.
    summary = describe_response(resp.text)
    print(f"refresh-clients OK: {resp.status_code} {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
