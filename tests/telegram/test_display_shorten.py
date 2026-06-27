"""beads-rbox D1/D2 site B: telegram display shortens peer name + notif token.

User-facing display strips the ``-<backend>`` suffix for ANY peer (the user
never addresses peers via notify_peer) and drops the ``#notif-`` marker entirely
(D2 2c — the user never authors an intent-ACK). The reply-map keeps the
canonical ``from_peer`` / ``notif-XXX`` so reply-context routing is unaffected.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from repowire.telegram.bot import TelegramPeer


def _make_peer() -> TelegramPeer:
    return TelegramPeer(
        bot_token="0:fake",
        chat_id="123",
        daemon_url="http://127.0.0.1:8377",
        state_path=Path("/nonexistent/telegram-state.json"),
    )


def _http_response(json_payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=200, text=str(json_payload), json=lambda: json_payload
    )


class _FakeHttp:
    def __init__(self, send_message_id: int) -> None:
        self.send_message_id = send_message_id
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict | None = None, **_: object):
        self.posts.append((url, json or {}))
        if "/sendMessage" in url:
            return _http_response(
                {"ok": True, "result": {"message_id": self.send_message_id}}
            )
        return _http_response({"ok": True, "result": {}})

    async def get(self, *a, **k):
        return _http_response({"ok": True, "result": []})

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _shown(fake: _FakeHttp) -> str:
    sends = [body for url, body in fake.posts if "/sendMessage" in url]
    assert len(sends) == 1
    return sends[0]["text"].replace("\\", "")  # strip MarkdownV2 escapes


async def test_notify_display_strips_suffix_and_drops_notif_marker():
    peer = _make_peer()
    fake = _FakeHttp(send_message_id=7)
    peer._http = fake  # type: ignore[assignment]

    await peer._on_ws(
        {
            "type": "notify",
            "from_peer": "director-claude-code",
            "text": "[#notif-deadbeef] hello there",
        }
    )

    shown = _shown(fake)
    assert "@director" in shown
    assert "director-claude-code" not in shown
    assert "[deadbeef]" in shown
    assert "[#notif-deadbeef]" not in shown

    # Reply-map keeps the canonical values (correlation invariant).
    assert peer._tg_msg_to_notif[7]["notif_id"] == "notif-deadbeef"
    assert peer._tg_msg_to_notif[7]["from_peer"] == "director-claude-code"
    assert peer._tg_msg_to_notif[7]["text"] == "[#notif-deadbeef] hello there"


async def test_notify_display_strips_regular_agent_name_too():
    # Unlike the pane site, telegram strips ANY peer's suffix (user-facing).
    peer = _make_peer()
    fake = _FakeHttp(send_message_id=8)
    peer._http = fake  # type: ignore[assignment]

    await peer._on_ws(
        {
            "type": "notify",
            "from_peer": "backend-worker-claude-code",
            "text": "[#notif-c0ffee00] done",
        }
    )

    shown = _shown(fake)
    assert "@backend-worker" in shown
    assert "backend-worker-claude-code" not in shown
    assert "[c0ffee00]" in shown
