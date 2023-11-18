"""Turn per-function usage into a memory recommendation and a cost estimate."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .inputs import FunctionUsage
from .pricing import Pricing

MIN_MEMORY_MB = 128
MAX_MEMORY_MB = 10240


@dataclass(frozen=True)
class Options:
    headroom: float = 0.20          # keep this much free above the highest Max Memory Used
    step_mb: int = 64               # round recommendations up to a multiple of this
    cpu_bound_fraction: float = 0.0  # share of billed time assumed to scale with 1/memory
    near_limit: float = 0.90        # flag memory or duration at or above this share of the limit
    min_invocations: int = 100      # below this the sample is flagged as too small
    timeout_seconds: float | None = None


@dataclass
class Recommendation:
    function: str
    invocations: int
    memory_mb: int
    max_memory_used_mb: float
    recommended_mb: int
    cost_now: float
    cost_recommended: float
    flags: list[str] = field(default_factory=list)

    @property
    def saving(self) -> float:
        """Estimated saving in USD over the sample; negative means the change costs more."""
        return self.cost_now - self.cost_recommended

    @property
    def used_fraction(self) -> float:
        return self.max_memory_used_mb / self.memory_mb

    @property
    def action(self) -> str:
        if self.recommended_mb < self.memory_mb:
            return "decrease"
        if self.recommended_mb > self.memory_mb:
            return "increase"
        return "keep"


def recommended_memory(max_used_mb: float, headroom: float, step_mb: int) -> int:
    needed = max_used_mb * (1.0 + headroom)
    rounded = math.ceil(needed / step_mb) * step_mb
    return int(min(MAX_MEMORY_MB, max(MIN_MEMORY_MB, rounded)))


def scaled_billed_ms(billed_ms: float, current_mb: float, new_mb: float, cpu_bound_fraction: float) -> float:
    """Billed time at a different memory size under the duration assumption.

    A fraction ``cpu_bound_fraction`` of the time is assumed to scale with
    current/new memory (Lambda allocates CPU in proportion to memory); the rest
    is assumed not to change. 0 means "duration does not change at all".
    """
    return billed_ms * ((1.0 - cpu_bound_fraction) + cpu_bound_fraction * current_mb / new_mb)


def recommend(usage: FunctionUsage, pricing: Pricing, options: Options = Options()) -> Recommendation:
    current = usage.memory_mb
    flags: list[str] = []
    target = recommended_memory(usage.max_memory_used_mb, options.headroom, options.step_mb)

    if usage.max_memory_used_mb >= current:
        flags.append("at-memory-limit")
    elif usage.max_memory_used_mb >= options.near_limit * current:
        flags.append("near-memory-limit")

    timeout = options.timeout_seconds or usage.timeout_seconds
    slow = False
    if usage.timeouts:
        flags.append(f"timed-out x{usage.timeouts}")
        slow = True
    elif timeout and usage.max_duration_ms >= options.near_limit * timeout * 1000:
        flags.append("near-timeout")
        slow = True
    if slow and target < current:
        # less memory also means less CPU, which makes a slow function slower
        target = current
        flags.append("kept: timeouts")

    if len(usage.memory_sizes) > 1:
        flags.append("memory-changed-in-sample")
    if usage.invocations < options.min_invocations:
        flags.append("small-sample")

    cost_now = usage.billed_mb_ms / 1000.0 / 1024.0 * pricing.per_gb_second \
        + usage.invocations * pricing.per_million_requests / 1_000_000
    if target == current and len(usage.memory_sizes) == 1:
        cost_new = cost_now
    else:
        billed = scaled_billed_ms(usage.billed_ms, current, target, options.cpu_bound_fraction)
        cost_new = pricing.cost(usage.invocations, billed, target)
    return Recommendation(
        function=usage.function,
        invocations=usage.invocations,
        memory_mb=current,
        max_memory_used_mb=usage.max_memory_used_mb,
        recommended_mb=target,
        cost_now=cost_now,
        cost_recommended=cost_new,
        flags=flags,
    )


def rank(recommendations: list[Recommendation]) -> list[Recommendation]:
    """Largest estimated saving first; functions that would cost more come last."""
    return sorted(recommendations, key=lambda r: (-r.saving, r.function))
