import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import datetime, timedelta, timezone

from agent_memory_gateway.scoring import decay_shadow, keyword_relevance, memory_score


class ScoringTests(unittest.TestCase):
    def test_decay_shadow_is_explanatory_and_does_not_claim_to_be_applied(self):
        result = decay_shadow(
            kind="temporary",
            created_at="2026-01-01T00:00:00Z",
            now=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["band"], "dead")
        self.assertLess(result["multiplier"], 0.15)

    def test_pinned_memory_has_a_shadow_floor(self):
        result = decay_shadow(
            kind="temporary",
            created_at="2020-01-01T00:00:00Z",
            pinned=True,
            now=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(result["band"], "hot")
        self.assertEqual(result["multiplier"], 0.85)

    def test_decay_shadow_normalizes_naive_and_offset_times(self):
        result = decay_shadow(
            kind="fact",
            created_at=datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))),
            now=datetime(2026, 1, 2),
        )

        self.assertEqual(result["age_days"], 1.0)

    def test_keyword_relevance_hits_query_terms(self):
        self.assertGreater(keyword_relevance("中文 注释", "默认使用中文文档和代码注释"), 0)

    def test_memory_score_is_positive_for_relevant_memory(self):
        score = memory_score(
            query="共享记忆",
            content="多 agent 共享记忆系统使用 Memory Gateway。",
            confidence=0.9,
            importance=0.8,
            created_at=None,
            half_life_days=90,
            access_count=2,
            scope_match=1.0,
        )
        self.assertGreater(score, 0)


if __name__ == "__main__":
    unittest.main()
