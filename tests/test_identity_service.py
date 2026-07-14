import base64
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_memory_gateway.auth import AuthError
from agent_memory_gateway.identity_service import (
    PairingAgent,
    _parse_refresh_credential,
    pairing_proof_message,
    verify_pairing_proof,
)


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class PairingProofTests(unittest.TestCase):
    def test_ed25519_proof_binds_code_device_and_nonce(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = encode(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        message = pairing_proof_message("pair-code", "pc-1", "nonce-1")
        signature = encode(private_key.sign(message))
        verify_pairing_proof(
            pairing_code="pair-code",
            device_id="pc-1",
            nonce="nonce-1",
            public_key=public_key,
            proof_signature=signature,
        )
        with self.assertRaises(AuthError) as raised:
            verify_pairing_proof(
                pairing_code="pair-code",
                device_id="pc-1",
                nonce="tampered",
                public_key=public_key,
                proof_signature=signature,
            )
        self.assertEqual(raised.exception.code, "PAIR_PROOF_INVALID")

    def test_agent_payload_and_refresh_format_are_strict(self):
        agent = PairingAgent.from_payload(
            {
                "agent_installation_id": "codex-pc",
                "agent_type": "codex",
                "display_name": "Codex on PC",
            }
        )
        self.assertEqual(agent.agent_type, "codex")
        credential_id, digest = _parse_refresh_credential("rfc_abc.secret")
        self.assertEqual(credential_id, "rfc_abc")
        self.assertEqual(len(digest), 64)
        with self.assertRaises(AuthError):
            _parse_refresh_credential("not-a-refresh")
        with self.assertRaises(AuthError):
            PairingAgent.from_payload(
                {
                    "agent_installation_id": "unknown-pc",
                    "agent_type": "unknown",
                    "display_name": "Unknown",
                }
            )


if __name__ == "__main__":
    unittest.main()
