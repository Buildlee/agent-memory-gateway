from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_memory_gateway.device_pair import (
    DevicePairError,
    DevicePairAgent,
    load_or_create_device_identity,
    pair_device,
    parse_agent_spec,
)
from agent_memory_gateway.file_credential import read_file_credential
from agent_memory_gateway.identity_service import verify_pairing_proof


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class DevicePairTests(unittest.TestCase):
    def test_parse_agent_spec_requires_readable_three_part_format(self) -> None:
        agent = parse_agent_spec("codex-desktop|codex|Codex Desktop")

        self.assertEqual(agent.agent_installation_id, "codex-desktop")
        self.assertEqual(agent.agent_type, "codex")
        self.assertEqual(agent.display_name, "Codex Desktop")
        with self.assertRaises(DevicePairError):
            parse_agent_spec("codex-desktop|codex")
        with self.assertRaises(DevicePairError):
            parse_agent_spec("codex-desktop|unknown|Codex Desktop")

    def test_device_identity_is_created_once_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.pem"

            created = load_or_create_device_identity(path)
            reused = load_or_create_device_identity(path)

            self.assertTrue(path.is_file())
            self.assertEqual(created.public_key, reused.public_key)
            self.assertEqual(len(created.public_key), 43)

    def test_pairing_signs_request_and_never_returns_refresh_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            device_key_file = Path(directory) / "device.pem"
            captured: dict[str, object] = {}

            def open_request(req, *, timeout, context):
                captured["payload"] = json.loads(req.data.decode("utf-8"))
                captured["timeout"] = timeout
                return _Response(
                    {
                        "device_id": "pc-01",
                        "agent_installation_ids": ["codex-desktop", "hermes-desktop"],
                        "refresh_credential": "rfc_test.credential-value-not-for-output",
                    }
                )

            agents = (
                DevicePairAgent("codex-desktop", "codex", "Codex Desktop"),
                DevicePairAgent("hermes-desktop", "hermes", "Hermes Desktop"),
            )
            with (
                patch("agent_memory_gateway.device_pair.read_generic_credential", return_value=None),
                patch("agent_memory_gateway.device_pair.write_generic_credential") as writer,
                patch("agent_memory_gateway.device_pair.request.urlopen", side_effect=open_request),
            ):
                result = pair_device(
                    gateway_url="https://gateway.example.test",
                    pairing_code="pair-code",
                    device_id="pc-01",
                    device_name="Local PC",
                    device_type="windows",
                    device_key_file=device_key_file,
                    agents=agents,
                    credential_target="AgentMemoryGateway/pc-01",
                    credential_username="tester",
                )

            payload = captured["payload"]
            self.assertIsInstance(payload, dict)
            verify_pairing_proof(
                pairing_code="pair-code",
                device_id="pc-01",
                nonce=str(payload["nonce"]),
                public_key=str(payload["public_key"]),
                proof_signature=str(payload["proof_signature"]),
            )
            self.assertEqual(captured["timeout"], 15)
            writer.assert_called_once_with(
                "AgentMemoryGateway/pc-01",
                "tester",
                "rfc_test.credential-value-not-for-output",
            )
            self.assertEqual(result["status"], "paired")
            self.assertNotIn("refresh_credential", result)
            self.assertNotIn("credential-value", json.dumps(result))

    def test_pairing_refuses_to_consume_code_when_credential_target_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "agent_memory_gateway.device_pair.read_generic_credential",
                    return_value=("tester", "existing-value"),
                ),
                patch("agent_memory_gateway.device_pair.request.urlopen") as urlopen,
            ):
                with self.assertRaises(FileExistsError):
                    pair_device(
                        gateway_url="https://gateway.example.test",
                        pairing_code="pair-code",
                        device_id="pc-01",
                        device_name="Local PC",
                        device_type="windows",
                        device_key_file=Path(directory) / "device.pem",
                        agents=(DevicePairAgent("codex-desktop", "codex", "Codex Desktop"),),
                        credential_target="AgentMemoryGateway/pc-01",
                        credential_username="tester",
                    )

            urlopen.assert_not_called()

    @unittest.skipIf(__import__("os").name == "nt", "受限文件凭据仅在 Linux/NAS 上启用")
    def test_pairing_can_store_a_linux_refresh_credential_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential_file = Path(directory) / "refresh.json"

            with patch(
                "agent_memory_gateway.device_pair.request.urlopen",
                return_value=_Response(
                    {
                        "device_id": "fn-hermes",
                        "agent_installation_ids": ["hermes-fn"],
                        "refresh_credential": "rfc_linux.credential-value-not-for-output",
                    }
                ),
            ):
                result = pair_device(
                    gateway_url="https://gateway.example.test",
                    pairing_code="pair-code",
                    device_id="fn-hermes",
                    device_name="FN Hermes",
                    device_type="nas",
                    device_key_file=Path(directory) / "device.pem",
                    agents=(DevicePairAgent("hermes-fn", "hermes", "FN Hermes"),),
                    credential_target=None,
                    credential_username="fn-hermes",
                    credential_file=credential_file,
                )

            self.assertEqual(result["credential_file"], str(credential_file))
            self.assertEqual(
                read_file_credential(credential_file),
                ("fn-hermes", "rfc_linux.credential-value-not-for-output"),
            )


if __name__ == "__main__":
    unittest.main()
