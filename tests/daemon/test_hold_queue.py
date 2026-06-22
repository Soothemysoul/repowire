"""beads-k1b3 (q3v5 L2): durable per-peer hold-queue spool.

While a subordinate is RESTARTING, notifies destined for it are appended to a
durable on-disk FIFO spool (per peer_id) instead of being rejected. The spool
survives a daemon restart (it is a file, not in-memory state) and is flushed in
order when the peer reconnects. A size/age bound guards against an unbounded
spool when a restart gets stuck — exceeding it is surfaced as a genuine
delivery failure to the sender.

Storage mirrors the nfap ack-state flock discipline: a per-peer lock file
serialises concurrent daemon writers so JSONL lines never interleave.
"""

from __future__ import annotations

import json

import pytest

from repowire.daemon import hold_queue


def _entry(text: str, cid: str | None = None) -> dict:
    return {
        "correlation_id": cid,
        "from_peer": "director-claude-code",
        "from_peer_id": "repow-global-aaaa1111",
        "from_peer_role": "orchestrator",
        "text": text,
        "interrupt": False,
    }


def test_enqueue_then_read_all_fifo(tmp_path):
    pid = "repow-dev-peer0001"
    hold_queue.enqueue(tmp_path, pid, _entry("first", "notif-00000001"), now=1000.0)
    hold_queue.enqueue(tmp_path, pid, _entry("second", "notif-00000002"), now=1001.0)
    entries = hold_queue.read_all(tmp_path, pid)
    assert [e["text"] for e in entries] == ["first", "second"]
    assert entries[0]["ts"] == 1000.0
    assert entries[0]["correlation_id"] == "notif-00000001"


def test_read_all_empty_when_no_spool(tmp_path):
    assert hold_queue.read_all(tmp_path, "repow-dev-ghost") == []


def test_clear_removes_spool(tmp_path):
    pid = "repow-dev-peer0002"
    hold_queue.enqueue(tmp_path, pid, _entry("x"), now=1.0)
    assert hold_queue.read_all(tmp_path, pid)
    hold_queue.clear(tmp_path, pid)
    assert hold_queue.read_all(tmp_path, pid) == []


def test_count(tmp_path):
    pid = "repow-dev-peer0003"
    assert hold_queue.count(tmp_path, pid) == 0
    hold_queue.enqueue(tmp_path, pid, _entry("a"), now=1.0)
    hold_queue.enqueue(tmp_path, pid, _entry("b"), now=2.0)
    assert hold_queue.count(tmp_path, pid) == 2


def test_replace_rewrites_tail(tmp_path):
    pid = "repow-dev-peer0004"
    hold_queue.enqueue(tmp_path, pid, _entry("a"), now=1.0)
    hold_queue.enqueue(tmp_path, pid, _entry("b"), now=2.0)
    remaining = hold_queue.read_all(tmp_path, pid)[1:]
    hold_queue.replace(tmp_path, pid, remaining)
    entries = hold_queue.read_all(tmp_path, pid)
    assert [e["text"] for e in entries] == ["b"]


def test_replace_empty_removes_spool(tmp_path):
    pid = "repow-dev-peer0005"
    hold_queue.enqueue(tmp_path, pid, _entry("a"), now=1.0)
    hold_queue.replace(tmp_path, pid, [])
    assert hold_queue.read_all(tmp_path, pid) == []


def test_enqueue_rejects_when_max_entries_exceeded(tmp_path):
    pid = "repow-dev-peer0006"
    for i in range(3):
        hold_queue.enqueue(tmp_path, pid, _entry(f"m{i}"), now=float(i), max_entries=3)
    with pytest.raises(hold_queue.HoldQueueFullError):
        hold_queue.enqueue(tmp_path, pid, _entry("overflow"), now=4.0, max_entries=3)
    # the rejected entry must NOT be appended
    assert hold_queue.count(tmp_path, pid) == 3


def test_enqueue_rejects_when_oldest_too_old(tmp_path):
    """A stuck restart: the oldest held message is older than the age bound →
    reject new notifies as a genuine delivery failure rather than holding more."""
    pid = "repow-dev-peer0007"
    hold_queue.enqueue(tmp_path, pid, _entry("old"), now=0.0, max_age_sec=100.0)
    with pytest.raises(hold_queue.HoldQueueFullError):
        hold_queue.enqueue(tmp_path, pid, _entry("new"), now=200.0, max_age_sec=100.0)


def test_spool_is_valid_jsonl(tmp_path):
    pid = "repow-dev-peer0008"
    hold_queue.enqueue(tmp_path, pid, _entry("hello"), now=5.0)
    path = hold_queue.spool_path(tmp_path, pid)
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "hello"


def test_peer_id_with_path_separators_sanitized(tmp_path):
    """A peer_id must never escape the spool dir via path separators."""
    pid = "../../etc/peer"
    hold_queue.enqueue(tmp_path, pid, _entry("x"), now=1.0)
    path = hold_queue.spool_path(tmp_path, pid)
    assert tmp_path in path.parents
