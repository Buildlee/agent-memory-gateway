import base64
import unittest
from contextlib import nullcontext

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_memory_gateway.auth import AuthError
from agent_memory_gateway.identity_service import (
    PairingAgent,
    IdentityAdmin,
    PostgresIdentityService,
    _normalize_agent_types,
    _parse_refresh_credential,
    _validate_identifier,
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

    def test_identifiers_reject_missing_or_non_text_values(self):
        for value in (None, 7, False):
            with self.subTest(value=value), self.assertRaises(AuthError) as raised:
                _validate_identifier("device_id", value)
            self.assertEqual(raised.exception.code, "DEVICE_ID_INVALID")

    def test_workspace_capabilities_reject_non_text_values(self):
        from agent_memory_gateway.identity_service import _normalize_capabilities

        for values in (("memory.search", 7), "memory.search", None):
            with self.subTest(values=values), self.assertRaises(AuthError) as raised:
                _normalize_capabilities(values)
            self.assertEqual(raised.exception.code, "WORKSPACE_CAPABILITIES_INVALID")

    def test_pairing_agent_types_reject_non_text_values(self):
        for values in (("codex", 7), "codex", None):
            with self.subTest(values=values), self.assertRaises(AuthError) as raised:
                _normalize_agent_types(values)
            self.assertEqual(raised.exception.code, "AGENT_TYPES_INVALID")

    def test_pairing_code_input_validation_fails_before_database_access(self):
        admin = IdentityAdmin("postgresql://unused")
        base = {
            "tenant_id": "personal",
            "user_id": "chlee",
            "allowed_device_type": "windows",
            "allowed_agent_types": ("codex",),
            "ttl_seconds": 600,
        }
        invalid_cases = (
            ({"allowed_device_type": True}, "DEVICE_TYPE_INVALID"),
            ({"allowed_agent_types": ("codex", 7)}, "AGENT_TYPES_INVALID"),
            ({"allowed_agent_types": None}, "AGENT_TYPES_INVALID"),
            ({"ttl_seconds": True}, "PAIRING_TTL_INVALID"),
            ({"ttl_seconds": "600"}, "PAIRING_TTL_INVALID"),
            (
                {"workspace_id": "workspace-a", "workspace_capabilities": "memory.search"},
                "WORKSPACE_CAPABILITIES_INVALID",
            ),
        )
        for changes, expected in invalid_cases:
            with self.subTest(changes=changes), self.assertRaises(AuthError) as raised:
                admin.create_pairing_code(**(base | changes))
            self.assertEqual(raised.exception.code, expected)

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
                    return Cursor(("pairing-1", "personal", "chlee", "windows", ["codex"], True, None, None, None))
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

    def test_pairing_applies_only_the_workspace_capabilities_in_the_code(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = encode(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        payload = {
            "pairing_code": "pair-code",
            "device_id": "windows-pc",
            "device_name": "WINDOWS-PC",
            "device_type": "windows",
            "public_key": public_key,
            "nonce": "nonce-1",
            "proof_signature": encode(private_key.sign(pairing_proof_message("pair-code", "windows-pc", "nonce-1"))),
            "agents": [
                {
                    "agent_installation_id": "codex-windows-pc",
                    "agent_type": "codex",
                    "display_name": "Codex",
                },
                {
                    "agent_installation_id": "hermes-windows-pc",
                    "agent_type": "hermes",
                    "display_name": "Hermes",
                },
            ],
        }

        class Cursor:
            def __init__(self, row=None, rowcount=1):
                self._row = row
                self.rowcount = rowcount

            def fetchone(self):
                return self._row

        class Connection:
            def __init__(self):
                self.bindings = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def transaction(self):
                return nullcontext()

            def execute(self, query, params):
                if "FROM pairing_codes" in query:
                    return Cursor(
                        (
                            "pairing-1",
                            "personal",
                            "chlee",
                            "windows",
                            ["codex", "hermes"],
                            True,
                            None,
                            "agent-memory-gateway",
                            ["memory.read_context", "memory.search", "memory.sync", "memory.write_event"],
                        )
                    )
                if "FROM workspaces" in query:
                    return Cursor(("personal", "chlee", "active"))
                if "WHERE device_id = %s" in query:
                    return Cursor()
                if "WHERE tenant_id = %s AND user_id = %s AND display_name = %s" in query:
                    return Cursor()
                if "FROM agent_installations" in query and "ANY(%s)" in query:
                    return Cursor()
                if "INSERT INTO workspace_bindings" in query:
                    self.bindings.append(params)
                return Cursor()

        connection = Connection()
        service = PostgresIdentityService(
            "postgres://test",
            object(),
            object(),
            connection_factory=lambda: connection,
        )
        result = service.pair(payload)

        self.assertEqual(result["workspace_id"], "agent-memory-gateway")
        self.assertEqual(
            result["workspace_capabilities"],
            ["memory.read_context", "memory.search", "memory.sync", "memory.write_event"],
        )
        self.assertEqual(
            connection.bindings,
            [
                (
                    "codex-windows-pc",
                    "agent-memory-gateway",
                    ["memory.read_context", "memory.search", "memory.sync", "memory.write_event"],
                ),
                (
                    "hermes-windows-pc",
                    "agent-memory-gateway",
                    ["memory.read_context", "memory.search", "memory.sync", "memory.write_event"],
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
