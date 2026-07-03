"""记忆召回评分。"""

from __future__ import annotations

import math
from datetime import datetime, timezone


DEFAULT_HALF_LIFE_DAYS: dict[str, float] = {
    "preference": 180,
    "fact": 90,
    "task_state": 14,
    "temporary": 3,
    "procedure": 365,
    "device_fact": 120,
}


def parse_time(value: str | None) -> datetime:
    """解析 ISO 时间，失败时返回当前时间。"""

    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def freshness(created_at: str | None, half_life_days: float) -> float:
    """根据半衰期计算新鲜度。"""

    created = parse_time(created_at)
    age_days = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
    half_life = max(1.0, float(half_life_days or 90))
    return math.exp(-age_days / half_life)


def keyword_relevance(query: str, content: str) -> float:
    """简单关键词相关性；正式版本应替换为 embedding 检索。"""

    query_terms = {term.lower() for term in (query or "").split() if term.strip()}
    if not query_terms:
        return 0.2
    content_lower = (content or "").lower()
    hits = sum(1 for term in query_terms if term in content_lower)
    return min(1.0, 0.15 + hits / max(1, len(query_terms)))


def memory_score(
    *,
    query: str,
    content: str,
    confidence: float,
    importance: float,
    created_at: str | None,
    half_life_days: float,
    access_count: int,
    scope_match: float,
) -> float:
    """计算记忆召回分数。"""

    relevance = keyword_relevance(query, content)
    fresh = freshness(created_at, half_life_days)
    reinforcement = 1.0 + min(0.5, max(0, access_count) * 0.03)
    return relevance * confidence * importance * fresh * reinforcement * scope_match
