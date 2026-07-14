import unittest

from agent_memory_gateway.access_token import AccessTokenError, AccessTokenSigner


class AccessTokenTests(unittest.TestCase):
    def signer(self, now: list[float]) -> AccessTokenSigner:
        return AccessTokenSigner(b"k" * 32, clock=lambda: now[0])

    def test_round_trip_and_expiry(self):
        now = [1_700_000_000.0]
        signer = self.signer(now)
        token, issued = signer.issue(
            tenant_id="personal",
            user_id="lee",
            device_id="pc",
            agent_installation_id="codex-pc",
            device_auth_epoch=3,
            agent_auth_epoch=4,
        )
        verified = signer.verify(token)
        self.assertEqual(verified, issued)
        self.assertEqual(verified.expires_at - verified.issued_at, 900)
        now[0] += 901
        with self.assertRaises(AccessTokenError):
            signer.verify(token)

    def test_tampering_and_wrong_key_are_rejected(self):
        now = [1_700_000_000.0]
        signer = self.signer(now)
        token, _ = signer.issue(
            tenant_id="personal",
            user_id="lee",
            device_id="pc",
            agent_installation_id="codex-pc",
            device_auth_epoch=1,
            agent_auth_epoch=1,
        )
        payload_parts = token.split(".")
        payload_parts[1] = ("A" if payload_parts[1][0] != "A" else "B") + payload_parts[1][1:]
        with self.assertRaises(AccessTokenError):
            signer.verify(".".join(payload_parts))
        with self.assertRaises(AccessTokenError):
            AccessTokenSigner(b"x" * 32, clock=lambda: now[0]).verify(token)

    def test_rejects_overlong_ttl_and_key_version_mismatch(self):
        with self.assertRaises(AccessTokenError):
            AccessTokenSigner(b"k" * 32, ttl_seconds=901)
        now = [1_700_000_000.0]
        token, _ = AccessTokenSigner(b"k" * 32, key_version="v1", clock=lambda: now[0]).issue(
            tenant_id="personal",
            user_id="lee",
            device_id="pc",
            agent_installation_id="codex-pc",
            device_auth_epoch=1,
            agent_auth_epoch=1,
        )
        with self.assertRaises(AccessTokenError):
            AccessTokenSigner(b"k" * 32, key_version="v2", clock=lambda: now[0]).verify(token)


if __name__ == "__main__":
    unittest.main()
