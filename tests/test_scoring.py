import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.scoring import keyword_relevance, memory_score


class ScoringTests(unittest.TestCase):
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
