from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities used by both cache implementations.
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit\s*card|ssn|social\s*security|user\s*\d+|account\s*\d+)\b",
    re.IGNORECASE,
)
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
UNCACHEABLE_RISKS = {"privacy", "high", "sensitive"}


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _metadata_uncacheable(metadata: dict[str, str] | None) -> bool:
    """Return True if caller metadata marks a response as unsafe to cache."""
    if not metadata:
        return False
    return metadata.get("expected_risk", "").lower() in UNCACHEABLE_RISKS


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


def _tokens(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _char_ngrams(text: str, size: int = 3) -> set[str]:
    compact = re.sub(r"\s+", " ", text.lower()).strip()
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


# ---------------------------------------------------------------------------
# In-memory cache.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Small in-memory response cache with TTL and false-hit guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.false_hit_log: list[dict[str, object]] = []
        self._entries: list[CacheEntry] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        best_seen_score = 0.0
        best_allowed_score = 0.0
        best_allowed_value: str | None = None
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        for entry in self._entries:
            score = self.similarity(query, entry.key)
            best_seen_score = max(best_seen_score, score)
            if _looks_like_false_hit(query, entry.key):
                if score >= self.similarity_threshold:
                    self.false_hit_log.append(
                        {"query": query, "cached_query": entry.key, "score": round(score, 4)}
                    )
                continue
            if score > best_allowed_score:
                best_allowed_score = score
                best_allowed_value = entry.value

        if best_allowed_score >= self.similarity_threshold:
            return best_allowed_value, best_allowed_score
        return None, best_seen_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query) or _metadata_uncacheable(metadata):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Deterministic lexical similarity with exact-match and n-gram signals."""
        left_norm = " ".join(_tokens(a))
        right_norm = " ".join(_tokens(b))
        if not left_norm or not right_norm:
            return 0.0
        if left_norm == right_norm:
            return 1.0

        token_score = _jaccard(set(left_norm.split()), set(right_norm.split()))
        char_score = _jaccard(_char_ngrams(left_norm), _char_ngrams(right_norm))
        return max(token_score, char_score * 0.95)


# ---------------------------------------------------------------------------
# Redis shared cache.
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis."""
        if _is_uncacheable(query):
            return None, 0.0

        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            exact = self._redis.hget(key, "response")
            if isinstance(exact, str):
                return exact, 1.0

            best_seen_score = 0.0
            best_allowed_score = 0.0
            best_allowed_value: str | None = None
            for redis_key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(redis_key, "query")
                cached_response = self._redis.hget(redis_key, "response")
                if not isinstance(cached_query, str) or not isinstance(cached_response, str):
                    continue

                score = ResponseCache.similarity(query, cached_query)
                best_seen_score = max(best_seen_score, score)
                if _looks_like_false_hit(query, cached_query):
                    if score >= self.similarity_threshold:
                        self.false_hit_log.append(
                            {
                                "query": query,
                                "cached_query": cached_query,
                                "score": round(score, 4),
                            }
                        )
                    continue
                if score > best_allowed_score:
                    best_allowed_score = score
                    best_allowed_value = cached_response

            if best_allowed_score >= self.similarity_threshold:
                return best_allowed_value, best_allowed_score
            return None, best_seen_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL."""
        if _is_uncacheable(query) or _metadata_uncacheable(metadata):
            return

        mapping = {"query": query, "response": value}
        if metadata:
            mapping["metadata"] = json.dumps(metadata, sort_keys=True)
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping=mapping)
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        """Remove all entries with this cache prefix."""
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                self._redis.delete(key)
        except Exception:
            return

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
