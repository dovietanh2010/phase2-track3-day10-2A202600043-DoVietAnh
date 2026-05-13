# Day 10 Reliability Final Report

## 1. Architecture Summary

The gateway checks cache first, then routes each miss through provider-specific circuit breakers. If the primary is unavailable, traffic moves through the fallback provider chain before returning a static degraded response.

```text
User -> Gateway -> Cache check -> cache hit
                    |
                    v
              Circuit breaker: primary -> Provider primary
                    | open/fail
                    v
              Circuit breaker: backup  -> Provider backup
                    | all fail
                    v
              Static fallback
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Detects persistent provider failure without opening on a single transient error. |
| reset_timeout_seconds | 1.0 | Gives the failed provider a short recovery window before HALF_OPEN probing. |
| success_threshold | 1 | One successful probe is enough for this fake provider lab workload. |
| cache TTL | 300 | Five minutes balances FAQ reuse with freshness. |
| similarity_threshold | 0.92 | High threshold keeps semantic cache hits conservative; guardrails catch date-sensitive false hits. |
| load_test requests | 100 | Enough samples to show circuit and cache behavior reproducibly. |

## 3. SLO Definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 1 | yes |
| Latency P95 | < 2500 ms | 518.88 | yes |
| Fallback success rate | >= 95% | 1 | yes |
| Cache hit rate | >= 10% | 0.405 | yes |
| Recovery time | < 5000 ms | 3199.2699 | yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 1 |
| error_rate | 0 |
| latency_p50_ms | 211.44 |
| latency_p95_ms | 518.88 |
| latency_p99_ms | 544.09 |
| fallback_success_rate | 1 |
| cache_hit_rate | 0.405 |
| circuit_open_count | 32 |
| recovery_time_ms | 3199.2699 |
| estimated_cost | 0.1055 |
| estimated_cost_saved | 0.1215 |

## 5. Cache Comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 202.46 | 0.23 | -99.9% |
| latency_p95_ms | 235.6 | 233.83 | -0.8% |
| estimated_cost | 0.017 | 0.0037 | -78.3% |
| cache_hit_rate | 0 | 0.7333 | +0.7333 |

## 6. Redis Shared Cache

In-memory cache is local to one gateway instance, so horizontally scaled workers would miss entries created by other workers. `SharedRedisCache` stores query/response hashes with TTL in Redis so separate gateway instances can share cache state.

### Evidence of shared state

```text
{
  "available": true,
  "shared_read": true,
  "score": 1.0,
  "keys": [
    "rl:cache:e6bb724160ee"
  ]
}
```

### Redis CLI output

```bash
docker compose exec redis redis-cli KEYS "rl:cache:*"
```

Observed keys:

```text
rl:cache:e6bb724160ee
```

## 7. Chaos Scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fails; backup serves traffic and circuit opens | availability 1, fallback success 1, circuit opens 25 | pass |
| primary_flaky_50 | Primary intermittently fails; breaker opens and fallback succeeds | availability 1, fallback success 1, circuit opens 7 | pass |
| all_healthy | Healthy primary handles misses; no circuit opens | availability 1, static fallbacks 0, circuit opens 0 | pass |
| cache_stale_candidate | Similar queries with different years do not return stale cache data | cache hit rate 0.82; different-year false hit prevented | pass |

## 8. Failure Analysis

The circuit breaker state is process-local. In production, multiple gateway replicas could disagree about whether a provider is unhealthy, so a failing provider might still receive traffic from replicas whose local breaker has not opened. I would move breaker counters and state transitions into Redis with atomic increments and expirations, then add per-provider metrics export.

## 9. Next Steps

1. Share circuit breaker state across instances using Redis or another low-latency coordination store.
2. Add concurrent load testing and Prometheus counters for request totals, latency, cache hits, and circuit state.
3. Add per-user cache partitioning and rate limits for privacy-sensitive production traffic.