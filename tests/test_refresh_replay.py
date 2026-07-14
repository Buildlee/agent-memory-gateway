import unittest

from agent_memory_gateway.refresh_replay import RefreshReplayCipher, RefreshReplayError


class RefreshReplayCipherTests(unittest.TestCase):
    def test_round_trip_is_bound_to_credential_id(self):
        cipher = RefreshReplayCipher(b"r" * 32)
        encrypted = cipher.encrypt("rfc_abc.new-secret", credential_id="rfc_abc")
        self.assertNotIn(b"new-secret", encrypted.ciphertext)
        self.assertEqual(
            cipher.decrypt(encrypted, credential_id="rfc_abc"),
            "rfc_abc.new-secret",
        )
        with self.assertRaises(RefreshReplayError):
            cipher.decrypt(encrypted, credential_id="rfc_other")

    def test_wrong_key_and_version_are_rejected(self):
        encrypted = RefreshReplayCipher(b"r" * 32, key_version="v1").encrypt(
            "rfc_abc.new-secret",
            credential_id="rfc_abc",
        )
        with self.assertRaises(RefreshReplayError):
            RefreshReplayCipher(b"x" * 32, key_version="v1").decrypt(
                encrypted,
                credential_id="rfc_abc",
            )
        with self.assertRaises(RefreshReplayError):
            RefreshReplayCipher(b"r" * 32, key_version="v2").decrypt(
                encrypted,
                credential_id="rfc_abc",
            )


if __name__ == "__main__":
    unittest.main()
