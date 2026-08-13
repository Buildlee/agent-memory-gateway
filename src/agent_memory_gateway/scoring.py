"""记忆召回评分。"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


DEFAULT_HALF_LIFE_DAYS: dict[str, float] = {
    "preference": 180,
    "fact": 90,
    "task_state": 14,
    "temporary": 3,
    "procedure": 365,
    "device_fact": 120,
}


def decay_shadow(
    *,
    kind: str,
    created_at: Any,
    last_useful_at: Any = None,
    pinned: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    """只计算遗忘曲线观察值；调用方不得把它用于正式排序。"""

    current = normalize_time(now or datetime.now(timezone.utc))
    created = parse_time_value(created_at, fallback=current)
    useful = parse_time_value(last_useful_at, fallback=created)
    anchor = max(created, useful)
    age_days = max(0.0, (current - anchor).total_seconds() / 86400)
    normalized_kind = str(kind or "fact").strip().lower()
    half_life = DEFAULT_HALF_LIFE_DAYS.get(normalized_kind, 90.0)
    multiplier = math.exp(-age_days / max(1.0, half_life))
    if pinned:
        multiplier = max(multiplier, 0.85)
    band = "hot" if multiplier >= 0.75 else "warm" if multiplier >= 0.40 else "cold" if multiplier >= 0.15 else "dead"
    return {
        "applied": False,
        "band": band,
        "multiplier": round(multiplier, 6),
        "age_days": round(age_days, 3),
        "half_life_days": half_life,
        "pinned_floor": bool(pinned),
    }


def parse_time_value(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return fallback
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return fallback
    return normalize_time(parsed)


def normalize_time(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


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
