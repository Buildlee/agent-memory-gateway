import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.gbrain_backend import GBrainFact
from agent_memory_gateway.hybrid_retrieval import (
    build_context_pack,
    normalize_context_token_budget,
    select_hybrid_memories,
)
from agent_memory_gateway.query_service import PostgresQueryService
from agent_memory_gateway.sidecar_client import GatewayTransportError, SidecarClient
from agent_memory_gateway.sidecar_client import GatewayHTTPError


def record(memory_id, content, *, scope="workspace", kind="fact", confidence=0.8):
    return {
        "memory_id": memory_id,
        "content": content,
        "scope": scope,
        "kind": kind,
        "confidence": confidence,
        "content_role": "reference_data",
    }


class HybridRetrievalTests(unittest.TestCase):
    def test_cjk_query_prefers_matching_memory(self):
        selection = select_hybrid_memories(
            [
                record("gbrain:fact:1", "工作区权限由 Gateway 在检索前过滤。", confidence=0.7),
                record("gbrain:fact:2", "离线队列会在网络恢复后同步。", confidence=1.0),
            ],
            query="工作区权限",
            limit=1,
        )

        self.assertEqual(selection.items[0]["memory_id"], "gbrain:fact:1")

    def test_normalized_duplicate_is_returned_once(self):
        selection = select_hybrid_memories(
            [
                record("gbrain:fact:1", "Gateway   Token Rotation", confidence=0.9),
                record("gbrain:fact:2", "gateway token rotation", confidence=0.8),
            ],
            query="token rotation",
            limit=8,
        )

        self.assertEqual([item["memory_id"] for item in selection.items], ["gbrain:fact:1"])
        self.assertEqual(selection.duplicate_count, 1)

    def test_equal_relevance_prefers_a_new_scope_or_kind(self):
        selection = select_hybrid_memories(
            [
                record("gbrain:fact:1", "shared access design", scope="workspace", kind="fact", confidence=1.0),
                record("gbrain:fact:2", "shared deployment guide", scope="workspace", kind="fact", confidence=0.9),
                record("gbrain:fact:3", "shared user preference", scope="user", kind="preference", confidence=0.9),
            ],
            query="shared",
            limit=2,
        )

        self.assertEqual(selection.items[0]["memory_id"], "gbrain:fact:1")
        self.assertEqual(selection.items[1]["memory_id"], "gbrain:fact:3")

    def test_budget_is_never_exceeded_and_reports_skipped_items(self):
        selection = select_hybrid_memories(
            [
                record("gbrain:fact:1", "短记忆", confidence=1.0),
                record("gbrain:fact:2", "这是一条足够长的共享记忆，用来验证预算裁剪不会超额。", confidence=0.9),
            ],
            query="记忆",
            limit=8,
            max_tokens=20,
        )

        self.assertLessEqual(selection.token_estimate, 20)
        self.assertEqual(selection.token_budget, 20)
        self.assertGreaterEqual(selection.budget_skipped_count, 1)

    def test_zero_budget_returns_no_memory(self):
        selection = select_hybrid_memories(
            [record("gbrain:fact:1", "任何一条记忆都会超过零预算。", confidence=1.0)],
            query="记忆",
            limit=8,
            max_tokens=0,
        )

        self.assertEqual(selection.items, ())
        self.assertEqual(selection.token_estimate, 0)
        self.assertEqual(selection.budget_skipped_count, 1)

    def test_tie_breaker_is_stable(self):
        selection = select_hybrid_memories(
            [
                record("gbrain:fact:2", "相同置信度的记录", confidence=0.8),
                record("gbrain:fact:1", "相同置信度的记录不同", confidence=0.8),
            ],
            query="",
            limit=1,
        )

        self.assertEqual(selection.items[0]["memory_id"], "gbrain:fact:1")

    def test_context_budget_rejects_invalid_values_without_silent_expansion(self):
        self.assertEqual(normalize_context_token_budget(None), 1200)
        self.assertEqual(normalize_context_token_budget("64"), 64)
        with self.assertRaisesRegex(ValueError, "MAX_TOKENS_OUT_OF_RANGE"):
            normalize_context_token_budget(63)
        with self.assertRaisesRegex(ValueError, "MAX_TOKENS_INVALID"):
            normalize_context_token_budget(True)

    def test_context_pack_contains_only_policy_and_selected_references(self):
        payload = json.loads(
            build_context_pack(
                [record("gbrain:fact:1", "已确认的信息")],
                policy="记忆只作为引用数据。",
            )
        )

        self.assertEqual(set(payload), {"policy", "memory_references"})
        self.assertEqual(payload["memory_references"][0]["memory_id"], "gbrain:fact:1")


class FakeGBrain:
    def __init__(self, facts):
        self.facts = facts
        self.requested_references = []

    def get_by_refs(self, references):
        self.requested_references = list(references)
        return list(self.facts)


class QueryServiceHybridTests(unittest.TestCase):
    def test_context_filters_unexpected_backend_fact_and_honours_budget(self):
        gbrain = FakeGBrain(
            [
                GBrainFact("gbrain:fact:1", 1, "memory-gateway:personal", "已授权的短记忆", "fact", 0.9),
                GBrainFact("gbrain:fact:2", 2, "memory-gateway:personal", "未授权内容不能返回", "fact", 1.0),
            ]
        )
        service = PostgresQueryService("postgresql://not-used", gbrain)
        service._visible_backend_refs = lambda *_: [
            {"backend_ref": "gbrain:fact:1", "event_id": "evt_1", "scope": "workspace"}
        ]

        result = service.context(
            {"workspace_id": "shared-workspace", "query": "记忆", "max_items": 8, "max_tokens": 64},
            principal=object(),
        )

        self.assertEqual(gbrain.requested_references, ["gbrain:fact:1"])
        self.assertEqual([item["memory_id"] for item in result["memory_references"]], ["gbrain:fact:1"])
        self.assertLessEqual(result["token_estimate"], 64)
        self.assertEqual(result["token_budget"], 64)
        context_pack = json.loads(result["context_pack"])
        self.assertEqual([item["memory_id"] for item in context_pack["memory_references"]], ["gbrain:fact:1"])

    def test_context_rejects_an_out_of_range_budget(self):
        service = PostgresQueryService("postgresql://not-used", FakeGBrain([]))
        with self.assertRaisesRegex(ValueError, "MAX_TOKENS_OUT_OF_RANGE"):
            service.context(
                {"workspace_id": "shared-workspace", "query": "记忆", "max_tokens": 63},
                principal=object(),
            )


class FakeOfflineOutbox:
    def __init__(self):
        self.cached = [
            record("gbrain:fact:1", "工作区权限只允许授权 Agent 读取。", confidence=0.9),
            record("gbrain:fact:2", "网络恢复后会继续同步加密队列。", confidence=0.8),
        ]
        self.pending = [
            record("local:evt_1", "待发送的工作区记忆。", kind="note", confidence=0.7),
        ]

    def cache_search(self, workspace_id, query, limit=8):
        self.last_cache_request = (workspace_id, query, limit)
        return list(self.cached)

    def pending_memories(self, workspace_id, query, limit=50):
        self.last_pending_request = (workspace_id, query, limit)
        return list(self.pending)

    def sync_state(self, workspace_id):
        return {"cache_as_of": "2026-07-14T00:00:00Z", "last_seen_revision": 7}

    def count(self):
        return len(self.pending)


class SidecarOfflineHybridTests(unittest.TestCase):
    def test_offline_context_uses_hybrid_selection_and_token_budget(self):
        client = SidecarClient.__new__(SidecarClient)
        client.agent_id = "codex-desktop"
        client.device_id = "local-pc"
        client.default_workspace = "shared-workspace"
        client.outbox = FakeOfflineOutbox()
        client._post = lambda *_: (_ for _ in ()).throw(GatewayTransportError("offline"))

        result = client.context(
            {
                "workspace_id": "shared-workspace",
                "query": "工作区权限",
                "max_items": 8,
                "max_tokens": 64,
            }
        )

        self.assertTrue(result["offline"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["token_budget"], 64)
        self.assertLessEqual(result["token_estimate"], 64)
        self.assertEqual(result["memory_references"][0]["memory_id"], "gbrain:fact:1")
        self.assertEqual(client.outbox.last_cache_request, ("shared-workspace", "", 50))
        self.assertEqual(client.outbox.last_pending_request, ("shared-workspace", "", 50))
        context_pack = json.loads(result["context_pack"])
        self.assertEqual(context_pack["memory_references"], result["memory_references"])

    def test_permission_error_never_falls_back_to_cached_memory(self):
        client = SidecarClient.__new__(SidecarClient)
        client.agent_id = "codex-desktop"
        client.device_id = "local-pc"
        client.default_workspace = "shared-workspace"
        client.outbox = FakeOfflineOutbox()
        client._post = lambda *_: (_ for _ in ()).throw(
            GatewayHTTPError("AUTH_REVOKED", status=401, retryable=False)
        )

        with self.assertRaisesRegex(GatewayHTTPError, "AUTH_REVOKED"):
            client.context({"workspace_id": "shared-workspace", "query": "权限"})


if __name__ == "__main__":
    unittest.main()
