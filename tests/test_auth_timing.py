import hashlib
import unittest
from unittest import mock

from agent_memory_gateway.auth import AuthError, Principal, TokenAuthenticator


def _principal(**overrides) -> Principal:
    return Principal(
        tenant_id=overrides.get("tenant_id", "personal"),
        user_id=overrides.get("user_id", "lee"),
        device_id=overrides.get("device_id", "pc"),
        agent_installation_id=overrides.get("agent_installation_id", "codex"),
        workspace_ids=frozenset(overrides.get("workspace_ids", ["workspace-a"])),
        capabilities=frozenset(overrides.get("capabilities", ["memory.search"])),
    )


class AuthTimingTests(unittest.TestCase):
    def test_hit_uses_dict_lookup(self):
        """authenticate 对已知 token 返回 principal，且只调用一次 dict.get。"""
        token = "valid-token"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expected = _principal()
        principals = {token_hash: expected}
        auth = TokenAuthenticator(principals)
        with mock.patch.object(auth, "_principals_by_hash", wraps=principals) as wrapped:
            result = auth.authenticate(f"Bearer {token}")
            self.assertIs(result, expected)
            wrapped.get.assert_called_once_with(token_hash)

    def test_miss_constant_time(self):
        """未命中时抛 TOKEN_INVALID，不用 AUTH_INVALID。"""
        known_hash = hashlib.sha256(b"known-token").hexdigest()
        auth = TokenAuthenticator({known_hash: _principal()})
        with self.assertRaises(AuthError) as raised:
            auth.authenticate("Bearer unknown-token")
        self.assertEqual(raised.exception.code, "TOKEN_INVALID")

    def test_invalid_token_format(self):
        """不合法认证头抛 AUTH_REQUIRED。"""
        auth = TokenAuthenticator({"a" * 64: _principal()})
        with self.assertRaises(AuthError) as raised:
            auth.authenticate(None)
        self.assertEqual(raised.exception.code, "AUTH_REQUIRED")

    def test_empty_token(self):
        """空令牌抛 AUTH_REQUIRED。"""
        auth = TokenAuthenticator({"a" * 64: _principal()})
        with self.assertRaises(AuthError) as raised:
            auth.authenticate("Bearer ")
        self.assertEqual(raised.exception.code, "AUTH_REQUIRED")


if __name__ == "__main__":
    unittest.main()
