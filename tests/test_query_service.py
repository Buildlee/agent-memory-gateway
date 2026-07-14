import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.gbrain_backend import GBrainFact
from agent_memory_gateway.query_service import PostgresQueryService


class QueryServiceResultTests(unittest.TestCase):
    def test_result_keeps_provenance_and_scope(self):
        fact = GBrainFact("gbrain:fact:42", 42, "memory-gateway:personal", "确认的决定", "commitment", 1.0)
        result = PostgresQueryService._fact_to_result(
            fact,
            {"backend_ref": "gbrain:fact:42", "event_id": "evt_1", "scope": "workspace"},
        )
        self.assertEqual(result["memory_id"], "gbrain:fact:42")
        self.assertEqual(result["source_event_id"], "evt_1")
        self.assertEqual(result["scope"], "workspace")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["content_role"], "reference_data")
        self.assertFalse(result["instruction_like"])
