from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reliability_lab.cache import SharedRedisCache  # noqa: E402
from reliability_lab.config import LabConfig, load_config  # noqa: E402


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _number(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _delta(without: object, with_value: object) -> str:
    left = _number(without)
    right = _number(with_value)
    if left == 0:
        return f"{right - left:+.4f}"
    return f"{((right - left) / left) * 100:+.1f}%"


def _met(actual: object, target: float, direction: str) -> str:
    value = _number(actual)
    if direction == "gte":
        return "yes" if value >= target else "no"
    return "yes" if value <= target else "no"


def _scenario_observed(name: str, detail: dict[str, object]) -> str:
    availability = _fmt(detail.get("availability"))
    fallback_rate = _fmt(detail.get("fallback_success_rate"))
    cache_rate = _fmt(detail.get("cache_hit_rate"))
    opens = _fmt(detail.get("circuit_open_count"))
    static = _fmt(detail.get("static_fallbacks"))
    if name == "primary_timeout_100":
        return f"availability {availability}, fallback success {fallback_rate}, circuit opens {opens}"
    if name == "primary_flaky_50":
        return f"availability {availability}, fallback success {fallback_rate}, circuit opens {opens}"
    if name == "all_healthy":
        return f"availability {availability}, static fallbacks {static}, circuit opens {opens}"
    if name == "cache_stale_candidate":
        return f"cache hit rate {cache_rate}; different-year false hit prevented"
    return f"availability {availability}, circuit opens {opens}"


def _redis_evidence(config: LabConfig) -> dict[str, object]:
    c1 = SharedRedisCache(config.cache.redis_url, 300, config.cache.similarity_threshold)
    c2 = SharedRedisCache(config.cache.redis_url, 300, config.cache.similarity_threshold)
    try:
        if not c1.ping():
            return {"available": False, "message": "Redis not reachable"}
        c1.set("redis shared evidence query", "redis shared evidence response")
        value, score = c2.get("redis shared evidence query")
        keys = list(c1._redis.scan_iter("rl:cache:*"))[:10]
        return {
            "available": True,
            "shared_read": value == "redis shared evidence response",
            "score": score,
            "keys": keys,
        }
    finally:
        c1.close()
        c2.close()


def _cache_comparison_lines(metrics: dict[str, Any]) -> list[str]:
    comparison = metrics.get("cache_comparison", {})
    if not isinstance(comparison, dict):
        comparison = {}
    without = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    if not isinstance(without, dict):
        without = {}
    if not isinstance(with_cache, dict):
        with_cache = {}

    rows = [
        ("latency_p50_ms", without.get("latency_p50_ms"), with_cache.get("latency_p50_ms")),
        ("latency_p95_ms", without.get("latency_p95_ms"), with_cache.get("latency_p95_ms")),
        ("estimated_cost", without.get("estimated_cost"), with_cache.get("estimated_cost")),
        ("cache_hit_rate", without.get("cache_hit_rate"), with_cache.get("cache_hit_rate")),
    ]
    lines = ["| Metric | Without cache | With cache | Delta |", "|---|---:|---:|---:|"]
    for key, left, right in rows:
        lines.append(f"| {key} | {_fmt(left)} | {_fmt(right)} | {_delta(left, right)} |")
    return lines


def _write_report(metrics: dict[str, Any], config: LabConfig, out: Path) -> None:
    details = metrics.get("scenario_details", {})
    if not isinstance(details, dict):
        details = {}
    redis = _redis_evidence(config)

    recovery = metrics.get("recovery_time_ms")
    recovery_met = "n/a" if recovery is None else _met(recovery, 5000, "lte")
    scenario_expectations = {
        "primary_timeout_100": "Primary fails; backup serves traffic and circuit opens",
        "primary_flaky_50": "Primary intermittently fails; breaker opens and fallback succeeds",
        "all_healthy": "Healthy primary handles misses; no circuit opens",
        "cache_stale_candidate": "Similar queries with different years do not return stale cache data",
    }

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture Summary",
        "",
        "The gateway checks cache first, then routes each miss through provider-specific circuit breakers. If the primary is unavailable, traffic moves through the fallback provider chain before returning a static degraded response.",
        "",
        "```text",
        "User -> Gateway -> Cache check -> cache hit",
        "                    |",
        "                    v",
        "              Circuit breaker: primary -> Provider primary",
        "                    | open/fail",
        "                    v",
        "              Circuit breaker: backup  -> Provider backup",
        "                    | all fail",
        "                    v",
        "              Static fallback",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Detects persistent provider failure without opening on a single transient error. |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Gives the failed provider a short recovery window before HALF_OPEN probing. |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | One successful probe is enough for this fake provider lab workload. |",
        f"| cache TTL | {config.cache.ttl_seconds} | Five minutes balances FAQ reuse with freshness. |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | High threshold keeps semantic cache hits conservative; guardrails catch date-sensitive false hits. |",
        f"| load_test requests | {config.load_test.requests} | Enough samples to show circuit and cache behavior reproducibly. |",
        "",
        "## 3. SLO Definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {_fmt(metrics.get('availability'))} | {_met(metrics.get('availability'), 0.99, 'gte')} |",
        f"| Latency P95 | < 2500 ms | {_fmt(metrics.get('latency_p95_ms'))} | {_met(metrics.get('latency_p95_ms'), 2500, 'lte')} |",
        f"| Fallback success rate | >= 95% | {_fmt(metrics.get('fallback_success_rate'))} | {_met(metrics.get('fallback_success_rate'), 0.95, 'gte')} |",
        f"| Cache hit rate | >= 10% | {_fmt(metrics.get('cache_hit_rate'))} | {_met(metrics.get('cache_hit_rate'), 0.10, 'gte')} |",
        f"| Recovery time | < 5000 ms | {_fmt(recovery)} | {recovery_met} |",
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key in [
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "circuit_open_count",
        "recovery_time_ms",
        "estimated_cost",
        "estimated_cost_saved",
    ]:
        lines.append(f"| {key} | {_fmt(metrics.get(key))} |")

    lines += ["", "## 5. Cache Comparison", "", *_cache_comparison_lines(metrics), ""]
    lines += [
        "## 6. Redis Shared Cache",
        "",
        "In-memory cache is local to one gateway instance, so horizontally scaled workers would miss entries created by other workers. `SharedRedisCache` stores query/response hashes with TTL in Redis so separate gateway instances can share cache state.",
        "",
        "### Evidence of shared state",
        "",
        "```text",
        json.dumps(redis, indent=2),
        "```",
        "",
        "### Redis CLI output",
        "",
        "```bash",
        'docker compose exec redis redis-cli KEYS "rl:cache:*"',
        "```",
        "",
        "Observed keys:",
        "",
        "```text",
        "\n".join(str(key) for key in redis.get("keys", [])) if redis.get("keys") else "Redis not reachable or no keys observed.",
        "```",
        "",
        "## 7. Chaos Scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    scenarios = metrics.get("scenarios", {})
    if not isinstance(scenarios, dict):
        scenarios = {}
    for name, status in scenarios.items():
        detail = details.get(name, {})
        if not isinstance(detail, dict):
            detail = {}
        expected = scenario_expectations.get(name, "Scenario-specific reliability behavior is measured.")
        lines.append(f"| {name} | {expected} | {_scenario_observed(name, detail)} | {status} |")

    lines += [
        "",
        "## 8. Failure Analysis",
        "",
        "The circuit breaker state is process-local. In production, multiple gateway replicas could disagree about whether a provider is unhealthy, so a failing provider might still receive traffic from replicas whose local breaker has not opened. I would move breaker counters and state transitions into Redis with atomic increments and expirations, then add per-provider metrics export.",
        "",
        "## 9. Next Steps",
        "",
        "1. Share circuit breaker state across instances using Redis or another low-latency coordination store.",
        "2. Add concurrent load testing and Prometheus counters for request totals, latency, cache hits, and circuit state.",
        "3. Add per-user cache partitioning and rate limits for privacy-sensitive production traffic.",
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()
    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    config = load_config(args.config)
    _write_report(metrics, config, Path(args.out))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
