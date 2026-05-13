from __future__ import annotations

import copy
import hashlib
import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive average OPEN-to-CLOSED recovery time from breaker transition logs."""
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _seed_for(name: str) -> int:
    return int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)


def _scenario_config(config: LabConfig, scenario: ScenarioConfig) -> LabConfig:
    scenario_config = copy.deepcopy(config)
    if scenario.name in {"primary_timeout_100", "primary_flaky_50"}:
        scenario_config.cache.enabled = False
    elif scenario.name == "cache_stale_candidate":
        scenario_config.cache.enabled = True
        scenario_config.cache.similarity_threshold = min(config.cache.similarity_threshold, 0.3)
    return scenario_config


def _cache_saved_cost(config: LabConfig) -> float:
    if not config.providers:
        return 0.001
    primary = config.providers[0]
    estimated_tokens = 75
    return estimated_tokens / 1000 * primary.cost_per_1k_tokens


def _record_result(metrics: RunMetrics, result: GatewayResponse, cache_saved_cost: float) -> None:
    metrics.total_requests += 1
    metrics.estimated_cost += result.estimated_cost
    metrics.latencies_ms.append(result.latency_ms)

    if result.cache_hit:
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += cache_saved_cost
        metrics.successful_requests += 1
    elif result.route.startswith("fallback:"):
        metrics.fallback_successes += 1
        metrics.successful_requests += 1
    elif result.route == "static_fallback":
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
    else:
        metrics.successful_requests += 1


def _run_cache_guardrail_probe(
    gateway: ReliabilityGateway,
    metrics: RunMetrics,
    cache_saved_cost: float,
) -> None:
    first = gateway.complete("Summarize refund policy for 2024 deadline")
    _record_result(metrics, first, cache_saved_cost)
    exact_hit = gateway.complete("Summarize refund policy for 2024 deadline")
    _record_result(metrics, exact_hit, cache_saved_cost)
    false_candidate = gateway.complete("Summarize refund policy for 2026 deadline")
    _record_result(metrics, false_candidate, cache_saved_cost)

    cache = gateway.cache
    false_hit_logged = False
    if isinstance(cache, (ResponseCache, SharedRedisCache)):
        false_hit_logged = bool(cache.false_hit_log)
    metrics.scenarios["cache_false_hit_prevented"] = (
        "pass" if not false_candidate.cache_hit and false_hit_logged else "fail"
    )


def _scenario_passed(scenario: ScenarioConfig, result: RunMetrics) -> bool:
    if scenario.name == "primary_timeout_100":
        return (
            result.availability >= 0.95
            and result.fallback_success_rate >= 0.95
            and result.circuit_open_count > 0
        )
    if scenario.name == "primary_flaky_50":
        return (
            result.availability >= 0.90
            and result.fallback_successes > 0
            and result.circuit_open_count > 0
        )
    if scenario.name == "all_healthy":
        return (
            result.availability >= 0.99
            and result.static_fallbacks == 0
            and result.circuit_open_count == 0
        )
    if scenario.name == "cache_stale_candidate":
        return result.scenarios.get("cache_false_hit_prevented") == "pass" and result.cache_hits > 0
    return result.successful_requests > 0


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    random.seed(_seed_for(scenario.name))
    scenario_config = _scenario_config(config, scenario)
    gateway = build_gateway(scenario_config, scenario.provider_overrides or None)
    cache = gateway.cache
    if isinstance(cache, SharedRedisCache):
        cache.flush()

    metrics = RunMetrics()
    request_count = scenario_config.load_test.requests
    cache_saved_cost = _cache_saved_cost(scenario_config)
    special_requests = 0

    if scenario.name == "cache_stale_candidate":
        _run_cache_guardrail_probe(gateway, metrics, cache_saved_cost)
        special_requests = 3

    for _ in range(max(0, request_count - special_requests)):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
        _record_result(metrics, result, cache_saved_cost)

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    metrics.scenario_details[scenario.name] = {
        "availability": round(metrics.availability, 4),
        "fallback_success_rate": round(metrics.fallback_success_rate, 4),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
        "circuit_open_count": metrics.circuit_open_count,
        "static_fallbacks": metrics.static_fallbacks,
        "recovery_time_ms": metrics.recovery_time_ms,
    }
    if isinstance(cache, SharedRedisCache):
        cache.close()
    return metrics


def _cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, object]]:
    request_count = min(30, config.load_test.requests)
    provider_overrides = {provider.name: 0.0 for provider in config.providers}
    scenario = ScenarioConfig(
        name="cache_comparison",
        description="Healthy providers with and without cache",
        provider_overrides=provider_overrides,
    )

    without_cache = copy.deepcopy(config)
    without_cache.cache.enabled = False
    without_cache.scenarios = []
    without_cache.load_test.requests = request_count

    with_cache = copy.deepcopy(config)
    with_cache.cache.enabled = True
    with_cache.cache.backend = "memory"
    with_cache.scenarios = []
    with_cache.load_test.requests = request_count

    no_cache_metrics = run_scenario(without_cache, queries, scenario)
    cache_metrics = run_scenario(with_cache, queries, scenario)
    return {
        "without_cache": no_cache_metrics.to_report_dict(),
        "with_cache": cache_metrics.to_report_dict(),
        "notes": {
            "requests": request_count,
            "provider_fail_rate": 0.0,
        },
    }


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        metrics.cache_comparison = _cache_comparison(config, queries)
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        passed = _scenario_passed(scenario, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_details.update(result.scenario_details)

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    combined.cache_comparison = _cache_comparison(config, queries)
    return combined
