from unittest.mock import patch

from repowire.relay.auth import APIKey, generate_api_key, validate_api_key


class TestRelayAuth:
    def test_generate_api_key(self):
        with patch.dict("os.environ", {"REPOWIRE_RELAY_SECRET": "test-secret"}):
            api_key = generate_api_key("user1", "test-key")

            assert api_key.key.startswith("rw_")
            assert api_key.user_id == "user1"
            assert api_key.name == "test-key"
            assert "user1" in api_key.key

    def test_validate_api_key(self):
        with patch.dict("os.environ", {"REPOWIRE_RELAY_SECRET": "test-secret"}):
            generated = generate_api_key("user1", "test")
            validated = validate_api_key(generated.key)

            assert validated is not None
            assert validated.user_id == "user1"
            assert validated.key == generated.key

    def test_validate_invalid_signature(self):
        with patch.dict("os.environ", {"REPOWIRE_RELAY_SECRET": "test-secret"}):
            result = validate_api_key("rw_user1_0000000000000000")
            assert result is None

    def test_validate_wrong_prefix(self):
        result = validate_api_key("bad_user1_abc")
        assert result is None

    def test_validate_malformed_key(self):
        result = validate_api_key("rw_nosignaturepart")
        assert result is None

    def test_different_secret_rejects(self):
        with patch.dict("os.environ", {"REPOWIRE_RELAY_SECRET": "secret-a"}):
            generated = generate_api_key("user1")

        with patch.dict("os.environ", {"REPOWIRE_RELAY_SECRET": "secret-b"}):
            result = validate_api_key(generated.key)
            assert result is None

    def test_dev_secret_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            api_key = generate_api_key("devuser")
            validated = validate_api_key(api_key.key)

            assert validated is not None
            assert validated.user_id == "devuser"

    def test_api_key_model(self):
        key = APIKey(key="rw_user1_abcdef0123456789", user_id="user1", name="test")
        assert key.key == "rw_user1_abcdef0123456789"
        assert key.name == "test"

    def test_user_id_with_underscores(self):
        """user_id containing underscores should work since we rsplit on last _."""
        with patch.dict("os.environ", {"REPOWIRE_RELAY_SECRET": "test-secret"}):
            generated = generate_api_key("org_team_user")
            validated = validate_api_key(generated.key)

            assert validated is not None
            assert validated.user_id == "org_team_user"

    def test_deterministic(self):
        """Same secret + user_id always produces the same key."""
        with patch.dict("os.environ", {"REPOWIRE_RELAY_SECRET": "test-secret"}):
            k1 = generate_api_key("user1")
            k2 = generate_api_key("user1")
            assert k1.key == k2.key
