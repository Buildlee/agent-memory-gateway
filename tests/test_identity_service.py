import base64
import unittest
from contextlib import nullcontext

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_memory_gateway.auth import AuthError
from agent_memory_gateway.identity_service import (
    PairingAgent,
    PostgresIdentityService,
    _parse_refresh_credential,
    pairing_proof_message,
    verify_pairing_proof,
)
from agent_memory_gateway.identity_cli import _capabilities


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

    def test_workspace_capability_parser_deduplicates_values(self):
        self.assertEqual(
            _capabilities("memory.search,memory.sync,memory.search"),
            ("memory.search", "memory.sync"),
        )

    def test_pairing_rejects_an_existing_display_name_before_insert(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = encode(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        payload = {
            "pairing_code": "pair-code",
            "device_id": "windows-pc-v2",
            "device_name": "WINDOWS-PC",
            "device_type": "windows",
            "public_key": public_key,
            "nonce": "nonce-1",
            "proof_signature": encode(private_key.sign(pairing_proof_message("pair-code", "windows-pc-v2", "nonce-1"))),
            "agents": [
                {
                    "agent_installation_id": "codex-windows-pc-v2",
                    "agent_type": "codex",
                    "display_name": "Codex",
                }
            ],
        }

        class Cursor:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def transaction(self):
                return nullcontext()

            def execute(self, query, params):
                if "FROM pairing_codes" in query:
                    return Cursor(("pairing-1", "personal", "chlee", "windows", ["codex"], True, None))
                if "WHERE device_id = %s" in query:
                    return Cursor(None)
                if "WHERE tenant_id = %s AND user_id = %s AND display_name = %s" in query:
                    return Cursor((1,))
                self.fail(f"unexpected query: {query}")

            def fail(self, message):
                raise AssertionError(message)

        service = PostgresIdentityService(
            "postgres://test",
            object(),
            object(),
            connection_factory=Connection,
        )
        with self.assertRaises(AuthError) as raised:
            service.pair(payload)
        self.assertEqual(raised.exception.code, "DEVICE_DISPLAY_NAME_CONFLICT")
        self.assertEqual(raised.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
