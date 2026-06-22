"""Durable per-peer hold-queue spool (beads-k1b3, q3v5 L2).

While a subordinate peer is ``PeerStatus.RESTARTING`` (self-restarting on
context-overflow), the daemon must NOT reject notifies destined for it — the
pane is briefly dead, but the peer is coming back. Instead each notify is
appended to a durable on-disk FIFO spool keyed by the peer's stable
``peer_id``. The spool is a plain JSONL file, so it survives a daemon restart;
on the peer's WS-reconnect the daemon flushes the spool in order and clears it.

Design notes
------------
* **Separation of concerns (Q1=(i)):** the hold-queue is its own daemon spool,
  deliberately NOT folded into the nfap ack-state — receipt-state and held
  payloads have different lifecycles, and blurring them would overload the
  receipt semantics the watchdog depends on.
* **Durability:** writes go straight to disk under an exclusive flock (mirrors
  the ack-state flock discipline in ``hooks/utils.py``) so concurrent daemon
  workers never interleave JSONL lines.
* **Bound:** a stuck restart must not grow the spool without limit. ``enqueue``
  rejects (raises :class:`HoldQueueFullError`) once the queue exceeds a size or age
  bound; the caller surfaces that to the sender as a genuine delivery failure
  (503), exactly as it would for an OFFLINE peer.

Time is passed in explicitly (``now``) so the spool logic stays deterministic
and unit-testable; callers pass ``time.time()``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bound defaults. The age bound is sized to the restart timescale (respawn +
# resume is minutes, not seconds) with headroom; past it the restart is
# considered stuck and further holds are rejected as real failures. Read lazily
# from the environment per-call so tests (and operator overrides) take effect
# without a re-import.
_FALLBACK_MAX_ENTRIES = 100
_FALLBACK_MAX_AGE_SEC = 900.0


def _env_max_entries() -> int:
    return int(os.environ.get("REPOWIRE_HOLDQ_MAX_ENTRIES", str(_FALLBACK_MAX_ENTRIES)))


def _env_max_age_sec() -> float:
    return float(os.environ.get("REPOWIRE_HOLDQ_MAX_AGE_SEC", str(_FALLBACK_MAX_AGE_SEC)))


class HoldQueueFullError(Exception):
    """Raised when a hold-queue spool exceeds its size/age bound on enqueue.

    Signals a stuck restart: the caller must treat the notify as a genuine
    delivery failure (escalate to the sender) rather than holding indefinitely.
    """


def holdq_dir() -> Path:
    """Default spool directory: ``$REPOWIRE_HOLDQ_DIR`` or ``<config>/holdq``."""
    override = os.environ.get("REPOWIRE_HOLDQ_DIR")
    if override:
        return Path(override)
    from repowire.config.models import Config

    return Config.get_config_dir() / "holdq"


def _sanitize_peer_id(peer_id: str) -> str:
    """Strip path separators so a peer_id can never escape the spool dir."""
    sanitized = peer_id.replace("/", "").replace("\\", "").replace("..", "")
    return sanitized or "unknown"


def spool_path(base_dir: Path, peer_id: str) -> Path:
    """Path to a peer's spool file under ``base_dir``."""
    return Path(base_dir) / f"{_sanitize_peer_id(peer_id)}.jsonl"


def _lock_path(base_dir: Path, peer_id: str) -> Path:
    path = spool_path(base_dir, peer_id)
    return path.with_suffix(path.suffix + ".lock")


def _read_lines_unlocked(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("hold-queue: skipping corrupt line in %s", path)
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError as e:
        logger.error("hold-queue: failed to read %s: %s", path, e)
    return out


def enqueue(
    base_dir: Path,
    peer_id: str,
    entry: dict[str, Any],
    *,
    now: float,
    max_entries: int | None = None,
    max_age_sec: float | None = None,
) -> None:
    """Append ``entry`` to the peer's spool (FIFO), stamping ``ts=now``.

    Raises :class:`HoldQueueFullError` — WITHOUT appending — when the existing spool
    already has ``>= max_entries`` entries, or its oldest entry is older than
    ``max_age_sec`` (a stuck restart). Bounds default to the environment-driven
    values when not passed explicitly. The bound check and the append happen
    under one exclusive flock so they are atomic against concurrent writers.
    """
    if max_entries is None:
        max_entries = _env_max_entries()
    if max_age_sec is None:
        max_age_sec = _env_max_age_sec()
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    path = spool_path(base_dir, peer_id)
    lock = _lock_path(base_dir, peer_id)
    with open(lock, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            existing = _read_lines_unlocked(path)
            if len(existing) >= max_entries:
                raise HoldQueueFullError(
                    f"hold-queue for {peer_id} full "
                    f"({len(existing)} >= {max_entries} entries)"
                )
            if existing:
                oldest_ts = min(float(e.get("ts", now)) for e in existing)
                if now - oldest_ts > max_age_sec:
                    raise HoldQueueFullError(
                        f"hold-queue for {peer_id} stale "
                        f"(oldest {now - oldest_ts:.0f}s > {max_age_sec:.0f}s)"
                    )
            record = dict(entry)
            record["ts"] = now
            with open(path, "a") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_all(base_dir: Path, peer_id: str) -> list[dict[str, Any]]:
    """Return all spooled entries for a peer in FIFO order (best-effort)."""
    base_dir = Path(base_dir)
    path = spool_path(base_dir, peer_id)
    if not path.exists():
        return []
    lock = _lock_path(base_dir, peer_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return _read_lines_unlocked(path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def count(base_dir: Path, peer_id: str) -> int:
    """Number of spooled entries for a peer."""
    return len(read_all(base_dir, peer_id))


def replace(base_dir: Path, peer_id: str, entries: list[dict[str, Any]]) -> None:
    """Atomically rewrite the spool to exactly ``entries`` (FIFO).

    Used after a partial flush to keep the undelivered tail. An empty list
    removes the spool entirely.
    """
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    path = spool_path(base_dir, peer_id)
    lock = _lock_path(base_dir, peer_id)
    with open(lock, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if not entries:
                _unlink(path)
                return
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w") as fh:
                for e in entries:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(path))
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def clear(base_dir: Path, peer_id: str) -> None:
    """Remove a peer's spool file (no-op if absent)."""
    base_dir = Path(base_dir)
    _unlink(spool_path(base_dir, peer_id))


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
