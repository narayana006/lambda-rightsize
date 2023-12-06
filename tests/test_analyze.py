import pytest

from lambda_rightsize.analyze import Options, rank, recommend, recommended_memory, scaled_billed_ms
from lambda_rightsize.inputs import FunctionUsage
from lambda_rightsize.pricing import DEFAULT_PRICE_PER_GB_SECOND, Pricing, make_pricing

PRICING = Pricing(per_gb_second=0.0000166667, per_million_requests=0.20)


def usage(name="fn", invocations=1000, billed_ms=100_000.0, memory=1024, used=200.0, max_duration=500.0,
          timeouts=0, timeout_seconds=None):
    u = FunctionUsage(name)
    u.add(max_duration, billed_ms, memory, used, count=invocations)
    u.timeouts = timeouts
    u.timeout_seconds = timeout_seconds
    return u


def test_pricing_formula():
    # 1,000,000 requests of 1000 ms at 1024 MB = 1,000,000 GB-seconds
    cost = PRICING.cost(1_000_000, 1_000_000 * 1000, 1024)
    assert cost == pytest.approx(16.6667 + 0.20)
    # 128 MB is one eighth of a GB
    assert PRICING.cost(0, 1000, 128) == pytest.approx(0.0000166667 / 8)


def test_make_pricing_defaults_and_overrides():
    assert make_pricing("arm64").per_gb_second == DEFAULT_PRICE_PER_GB_SECOND["arm64"]
    assert "us-east-1" in make_pricing().source
    custom = make_pricing("x86_64", per_gb_second=0.00002, per_million_requests=0.25)
    assert (custom.per_gb_second, custom.per_million_requests) == (0.00002, 0.25)
    assert custom.source == "set on the command line"
    assert make_pricing(per_million_requests=0).per_million_requests == 0


def test_recommended_memory_rounds_up_and_respects_limits():
    assert recommended_memory(200, 0.20, 64) == 256       # 240 -> 256
    assert recommended_memory(160, 0.20, 64) == 192       # exactly 192
    assert recommended_memory(40, 0.20, 64) == 128        # Lambda minimum
    assert recommended_memory(9000, 0.20, 64) == 10240    # Lambda maximum
    assert recommended_memory(200, 0.0, 1) == 200


def test_decrease_saves_in_proportion_to_memory_when_duration_is_constant():
    result = recommend(usage(), PRICING)
    assert result.action == "decrease"
    assert result.recommended_mb == 256
    compute_now = 100 * 1.0 * 0.0000166667           # 100 s at 1 GB
    assert result.cost_now == pytest.approx(compute_now + 1000 * 0.20 / 1e6)
    assert result.saving == pytest.approx(compute_now * 0.75)
    assert result.flags == []


def test_fully_cpu_bound_function_saves_nothing():
    result = recommend(usage(), PRICING, Options(cpu_bound_fraction=1.0))
    assert result.recommended_mb == 256
    assert result.saving == pytest.approx(0.0, abs=1e-12)
    assert scaled_billed_ms(100, 1024, 256, 0.5) == pytest.approx(250)


def test_keep_when_already_right_sized():
    result = recommend(usage(memory=256, used=181), PRICING)
    assert result.action == "keep"
    assert result.saving == 0


def test_near_and_at_memory_limit_suggest_an_increase_that_costs_more():
    near = recommend(usage(memory=2048, used=1893), PRICING)
    assert near.flags == ["near-memory-limit"]
    assert near.recommended_mb == 2304 and near.action == "increase"
    assert near.saving < 0
    at = recommend(usage(memory=512, used=512), PRICING)
    assert at.flags == ["at-memory-limit"]
    assert at.recommended_mb == 640


def test_timeouts_block_a_decrease():
    result = recommend(usage(timeouts=2, timeout_seconds=3.0), PRICING)
    assert result.action == "keep"
    assert result.flags == ["timed-out x2", "kept: timeouts"]
    assert result.saving == 0


def test_near_timeout_needs_a_known_timeout():
    slow = usage(max_duration=2800.0)
    assert recommend(slow, PRICING).action == "decrease"
    flagged = recommend(slow, PRICING, Options(timeout_seconds=3.0))
    assert flagged.flags == ["near-timeout", "kept: timeouts"]
    assert flagged.action == "keep"


def test_small_sample_and_changed_memory_flags():
    small = recommend(usage(invocations=14), PRICING)
    assert "small-sample" in small.flags
    mixed = usage()
    mixed.add(100, 1000, 512, 150)
    assert "memory-changed-in-sample" in recommend(mixed, PRICING).flags


def test_rank_orders_by_saving():
    big = recommend(usage("big", billed_ms=10_000_000), PRICING)
    small = recommend(usage("small"), PRICING)
    worse = recommend(usage("worse", memory=512, used=512), PRICING)
    keep = recommend(usage("keep", memory=256, used=181), PRICING)
    assert [r.function for r in rank([worse, small, keep, big])] == ["big", "small", "keep", "worse"]
