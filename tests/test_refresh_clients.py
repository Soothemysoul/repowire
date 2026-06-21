import pytest

from scripts.repowire_refresh_clients import build_request


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
    assert body == {"reason": "deploy sha=abc", "scope": "workers"}  # no target_epoch (daemon-derived)


def test_build_request_without_token_omits_auth():
    _, _, headers, _ = build_request(
        daemon_url="http://127.0.0.1:8377", reason="r", scope="all", token=None
    )
    assert "Authorization" not in headers


def test_build_request_rejects_bad_scope():
    with pytest.raises(ValueError):
        build_request(daemon_url="http://x", reason="r", scope="everyone", token=None)
