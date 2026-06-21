import pytest

from scripts.repowire_refresh_clients import build_request, describe_response


def test_build_request_with_token_sets_bearer():
    method, url, headers, body = build_request(
        daemon_url="http://127.0.0.1:8377",
        reason="deploy sha=abc",
        scope="workers",
        token="secret",
    )
    assert method == "POST"
    assert url == "http://127.0.0.1:8377/control/refresh-clients"
    assert headers["Authorization"] == "Bearer secret"
    # no target_epoch — daemon-derived post-restart
    assert body == {"reason": "deploy sha=abc", "scope": "workers"}


def test_build_request_without_token_omits_auth():
    _, _, headers, _ = build_request(
        daemon_url="http://127.0.0.1:8377", reason="r", scope="all", token=None
    )
    assert "Authorization" not in headers


def test_build_request_rejects_bad_scope():
    with pytest.raises(ValueError):
        build_request(daemon_url="http://x", reason="r", scope="everyone", token=None)


def test_describe_response_renders_notified_and_epoch():
    summary = describe_response('{"notified": 3, "target_epoch": "v0.9.1+abc"}')
    assert summary == "notified=3 target_epoch=v0.9.1+abc"


def test_describe_response_falls_back_on_lean_body():
    assert describe_response('{"ok": true}') == '{"ok": true}'


def test_describe_response_falls_back_on_non_json():
    assert describe_response("not json at all") == "not json at all"
