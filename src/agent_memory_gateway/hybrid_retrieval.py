"""不依赖外部模型的混合检索、去重、多样性和 token 预算。"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{1,}")
_SPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True)
class HybridSelection:
    """一次选择的可审计结果；所有 token 均为保守估算单位。"""

    items: tuple[dict[str, Any], ...]
    candidate_count: int
    duplicate_count: int
    budget_skipped_count: int
    token_estimate: int
    token_budget: int | None

    def metadata(self) -> dict[str, int | None]:
        return {
            "candidate_count": self.candidate_count,
            "duplicate_count": self.duplicate_count,
            "budget_skipped_count": self.budget_skipped_count,
            "token_estimate": self.token_estimate,
            "token_budget": self.token_budget,
        }


def normalize_text(value: str) -> str:
    """统一 Unicode、大小写和空白，避免相同文本因格式不同而重复。"""

    return _SPACE_PATTERN.sub(" ", unicodedata.normalize("NFKC", str(value)).casefold()).strip()


def estimate_tokens(value: str, *, item_overhead: int = 8) -> int:
    """返回保守 token 估算，优先保证 Gateway 预算不会被超量选择。"""

    text = normalize_text(value)
    estimate = item_overhead
    index = 0
    while index < len(text):
        char = text[index]
        if _CJK_PATTERN.fullmatch(char):
            estimate += 1
            index += 1
            continue
        if char.isascii() and char.isalnum():
            end = index + 1
            while end < len(text) and text[end].isascii() and text[end].isalnum():
                end += 1
            estimate += max(1, math.ceil((end - index) / 3))
            index = end
            continue
        if not char.isspace():
            estimate += 1
        index += 1
    return estimate


def _feature_counts(value: str) -> Counter[str]:
    text = normalize_text(value)
    features: Counter[str] = Counter()
    for word in _WORD_PATTERN.findall(text):
        features[f"word:{word}"] += 1
    cjk_chars = [char for char in text if _CJK_PATTERN.fullmatch(char)]
    for char in cjk_chars:
        features[f"char:{char}"] += 1
    for left, right in zip(cjk_chars, cjk_chars[1:]):
        features[f"bigram:{left}{right}"] += 1
    if not features and text:
        features[f"text:{text}"] = 1
    return features


def _hashed_vector(features: Mapping[str, int], *, dimensions: int = 257) -> dict[int, float]:
    vector: dict[int, float] = {}
    for feature, count in features.items():
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] = vector.get(index, 0.0) + 1.0 + math.log(max(1, count))
    return vector


def _cosine(left: Mapping[int, float], right: Mapping[int, float]) -> float:
    if not left or not right:
        return 0.0
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    dot = sum(value * larger.get(index, 0.0) for index, value in smaller.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _lexical_score(query: str, content: str, query_features: Mapping[str, int], content_features: Mapping[str, int]) -> float:
    if not query_features:
        return 0.0
    shared = sum(min(count, content_features.get(feature, 0)) for feature, count in query_features.items())
    query_total = sum(query_features.values())
    union = sum(query_features.values()) + sum(content_features.values()) - shared
    coverage = shared / query_total if query_total else 0.0
    jaccard = shared / union if union else 0.0
    phrase = 1.0 if query and query in content else 0.0
    return 0.45 * coverage + 0.25 * jaccard + 0.30 * phrase


@dataclass(frozen=True)
class _ScoredCandidate:
    record: dict[str, Any]
    normalized_content: str
    vector: dict[int, float]
    base_score: float
    group: str
    tokens: int


def select_hybrid_memories(
    records: Sequence[Mapping[str, Any]],
    *,
    query: str,
    limit: int,
    max_tokens: int | None = None,
) -> HybridSelection:
    """以全文/CJK 特征和稳定哈希向量混合重排，再做 MMR 与严格预算。"""

    bounded_limit = max(1, min(int(limit), 50))
    budget = None if max_tokens is None else max(0, int(max_tokens))
    normalized_query = normalize_text(query)
    query_features = _feature_counts(normalized_query)
    query_vector = _hashed_vector(query_features)
    scored: list[_ScoredCandidate] = []
    for source in records:
        item = dict(source)
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        normalized_content = normalize_text(content)
        features = _feature_counts(normalized_content)
        vector = _hashed_vector(features)
        lexical = _lexical_score(normalized_query, normalized_content, query_features, features)
        vector_score = _cosine(query_vector, vector)
        confidence = min(1.0, max(0.0, float(item.get("confidence") or 0.0)))
        base_score = confidence if not normalized_query else 0.50 * lexical + 0.35 * vector_score + 0.15 * confidence
        item["retrieval_score"] = round(base_score, 6)
        group = f"{item.get('scope') or ''}:{item.get('kind') or ''}"
        scored.append(
            _ScoredCandidate(
                record=item,
                normalized_content=normalized_content,
                vector=vector,
                base_score=base_score,
                group=group,
                tokens=estimate_tokens(content),
            )
        )

    scored.sort(key=lambda candidate: (-candidate.base_score, str(candidate.record.get("memory_id") or "")))
    unique: list[_ScoredCandidate] = []
    duplicate_count = 0
    for candidate in scored:
        if any(
            candidate.normalized_content == existing.normalized_content
            or _cosine(candidate.vector, existing.vector) >= 0.94
            for existing in unique
        ):
            duplicate_count += 1
            continue
        unique.append(candidate)

    selected: list[_ScoredCandidate] = []
    remaining = list(unique)
    budget_skipped_count = 0
    remaining_budget = budget
    while remaining and len(selected) < bounded_limit:
        def mmr(candidate: _ScoredCandidate) -> tuple[float, str]:
            similarity = max((_cosine(candidate.vector, existing.vector) for existing in selected), default=0.0)
            group_penalty = 0.04 if any(candidate.group == existing.group for existing in selected) else 0.0
            return (0.80 * candidate.base_score - 0.20 * similarity - group_penalty, str(candidate.record.get("memory_id") or ""))

        candidate = max(remaining, key=mmr)
        remaining.remove(candidate)
        if remaining_budget is not None and candidate.tokens > remaining_budget:
            budget_skipped_count += 1
            continue
        adjusted_score = mmr(candidate)[0]
        item = dict(candidate.record)
        item["retrieval_score"] = round(adjusted_score, 6)
        item["token_estimate"] = candidate.tokens
        selected.append(
            _ScoredCandidate(
                record=item,
                normalized_content=candidate.normalized_content,
                vector=candidate.vector,
                base_score=candidate.base_score,
                group=candidate.group,
                tokens=candidate.tokens,
            )
        )
        if remaining_budget is not None:
            remaining_budget -= candidate.tokens

    return HybridSelection(
        items=tuple(dict(candidate.record) for candidate in selected),
        candidate_count=len(scored),
        duplicate_count=duplicate_count,
        budget_skipped_count=budget_skipped_count,
        token_estimate=sum(candidate.tokens for candidate in selected),
        token_budget=budget,
    )
