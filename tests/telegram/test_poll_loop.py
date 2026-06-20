"""Unit tests for TelegramPeer._poll_loop offset semantics (beads-mhph).

Regression guard for the at-least-once delivery fix: the getUpdates offset
must only advance AFTER ``_on_update`` succeeds. A transient delivery failure
(e.g. daemon reconnect window during a restart) must NOT advance the offset
for the failing update, so Telegram redelivers it on the next getUpdates.
Before the fix the offset was advanced first, so a failing delivery silently
dropped the user's message forever.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repowire.telegram.bot import TelegramPeer


def _make_peer(tmp_path: Path) -> TelegramPeer:
    return TelegramPeer(
        bot_token="0:fake",
        chat_id="999",
        daemon_url="http://127.0.0.1:8377",
        state_path=tmp_path / "telegram-state.json",
    )


def _resp(updates: list[dict]) -> MagicMock:
    """Fake httpx response whose .json() returns a getUpdates payload."""
    r = MagicMock()
    r.json.return_value = {"result": updates}
    return r


def _offsets_passed(get_mock: AsyncMock) -> list[int]:
    """Offset query-param recorded on each getUpdates call, in order."""
    return [c.kwargs["params"]["offset"] for c in get_mock.call_args_list]


@pytest.mark.asyncio
async def test_failed_delivery_does_not_advance_offset_and_redelivers(tmp_path):
    """update delivery fails → offset NOT advanced → next getUpdates re-requests
    with the same offset (redelivery). Direct repro of message loss."""
    peer = _make_peer(tmp_path)
    first_id = 100
    initial_offset = peer._tg_offset  # 0

    calls = {"n": 0}

    def get_side_effect(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp([{"update_id": first_id, "message": {}}])
        # Second poll: stop the loop, return nothing new.
        peer._stopping = True
        return _resp([])

    peer._http.get = AsyncMock(side_effect=get_side_effect)
    peer._on_update = AsyncMock(side_effect=RuntimeError("daemon unreachable"))

    with patch("repowire.telegram.bot.asyncio.sleep", new=AsyncMock()):
        await peer._poll_loop()

    # _on_update was attempted for the update.
    peer._on_update.assert_awaited_once()
    assert peer._on_update.await_args.args[0]["update_id"] == first_id

    # Offset NOT advanced for the failed update.
    assert peer._tg_offset == initial_offset, "offset must not advance on failure"

    # The second getUpdates re-requests with the same (un-advanced) offset.
    offsets = _offsets_passed(peer._http.get)
    assert len(offsets) == 2
    assert offsets[0] == initial_offset
    assert offsets[1] == initial_offset, "failed update must be redelivered"


@pytest.mark.asyncio
async def test_happy_path_advances_offset_past_batch_in_order(tmp_path):
    """Successful batch [first, first+1] → offset == (first+1)+1; _on_update
    called once per update, in order. Happy path must not regress."""
    peer = _make_peer(tmp_path)
    first_id = 100

    calls = {"n": 0}

    def get_side_effect(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(
                [{"update_id": first_id, "message": {}}, {"update_id": first_id + 1, "message": {}}]
            )
        peer._stopping = True
        return _resp([])

    peer._http.get = AsyncMock(side_effect=get_side_effect)
    peer._on_update = AsyncMock()

    with patch("repowire.telegram.bot.asyncio.sleep", new=AsyncMock()):
        await peer._poll_loop()

    assert peer._tg_offset == first_id + 2
    delivered = [c.args[0]["update_id"] for c in peer._on_update.await_args_list]
    assert delivered == [first_id, first_id + 1]


@pytest.mark.asyncio
async def test_partial_batch_acks_only_succeeded_prefix(tmp_path):
    """Batch [first ok, first+1 fail] → offset == first+1 (only first acked);
    first+1 redelivered, first is not."""
    peer = _make_peer(tmp_path)
    first_id = 100

    calls = {"n": 0}

    def get_side_effect(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(
                [{"update_id": first_id, "message": {}}, {"update_id": first_id + 1, "message": {}}]
            )
        peer._stopping = True
        return _resp([])

    def on_update_side_effect(u):
        if u["update_id"] == first_id + 1:
            raise RuntimeError("daemon unreachable")

    peer._http.get = AsyncMock(side_effect=get_side_effect)
    peer._on_update = AsyncMock(side_effect=on_update_side_effect)

    with patch("repowire.telegram.bot.asyncio.sleep", new=AsyncMock()):
        await peer._poll_loop()

    # Only the first update was acked; first+1 stays un-acked for redelivery.
    assert peer._tg_offset == first_id + 1
    offsets = _offsets_passed(peer._http.get)
    assert offsets[1] == first_id + 1, "second poll re-requests starting at the failed update"
