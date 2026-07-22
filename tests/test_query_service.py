import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.gbrain_backend import GBrainFact
from agent_memory_gateway.query_service import PostgresQueryService
from agent_memory_gateway.auth import Principal


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

    def test_feedback_adjustment_is_bounded_before_ranking(self):
        fact = GBrainFact("gbrain:fact:42", 42, "memory-gateway:personal", "已确认记忆", "fact", 0.8)
        promoted = PostgresQueryService._fact_to_result(
            fact,
            {
                "backend_ref": "gbrain:fact:42",
                "event_id": "evt_1",
                "scope": "workspace",
                "feedback_adjustment": 2.0,
            },
        )
        demoted = PostgresQueryService._fact_to_result(
            fact,
            {
                "backend_ref": "gbrain:fact:42",
                "event_id": "evt_1",
                "scope": "workspace",
                "feedback_adjustment": -2.0,
            },
        )
        self.assertEqual(promoted["feedback_adjustment"], 0.09)
        self.assertEqual(promoted["confidence"], 0.89)
        self.assertEqual(demoted["feedback_adjustment"], -0.24)
        self.assertEqual(demoted["confidence"], 0.56)

    def test_recall_record_only_contains_query_hash_and_memory_references(self):
        class Connection:
            def __init__(self):
                self.params = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, _query, params):
                self.params = params

        connection = Connection()
        service = PostgresQueryService(
            "postgresql://not-used",
            object(),
            connection_factory=lambda: connection,
        )
        actor = Principal(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            agent_installation_id="agent-a",
            workspace_ids=frozenset({"workspace-a"}),
            capabilities=frozenset({"memory.read_context"}),
            workspace_capabilities={"workspace-a": frozenset({"memory.read_context"})},
        )

        service._record_recall(
            actor,
            "workspace-a",
            "tr_recall",
            "不要写入数据库的原始查询",
            ["gbrain:fact:1"],
        )

        serialized = repr(connection.params)
        self.assertNotIn("不要写入数据库的原始查询", serialized)
        self.assertIn("gbrain:fact:1", serialized)
        self.assertIn("tr_recall", serialized)
