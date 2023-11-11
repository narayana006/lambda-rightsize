"""Lambda price inputs.

The defaults below are the public on-demand prices for US East (N. Virginia),
first pricing tier, as published on https://aws.amazon.com/lambda/pricing/
when this file was written. Prices differ by region and change over time, and
the free tier, the volume tiers, Savings Plans, provisioned concurrency and
ephemeral storage are not modelled. Override them on the command line.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PRICE_PER_GB_SECOND = {
    "x86_64": 0.0000166667,
    "arm64": 0.0000133334,
}
DEFAULT_PRICE_PER_MILLION_REQUESTS = 0.20
DEFAULT_PRICE_SOURCE = "us-east-1 on-demand list price, first tier, no free tier"


@dataclass(frozen=True)
class Pricing:
    per_gb_second: float
    per_million_requests: float
    source: str = DEFAULT_PRICE_SOURCE

    def cost(self, invocations: int, billed_ms: float, memory_mb: float) -> float:
        """Cost in USD of `invocations` requests billed `billed_ms` in total at `memory_mb`.

        Lambda bills GB-seconds with 1 GB = 1024 MB.
        """
        gb_seconds = (billed_ms / 1000.0) * (memory_mb / 1024.0)
        return gb_seconds * self.per_gb_second + invocations * self.per_million_requests / 1_000_000


def make_pricing(arch: str = "x86_64", per_gb_second: float | None = None,
                 per_million_requests: float | None = None) -> Pricing:
    custom = per_gb_second is not None or per_million_requests is not None
    return Pricing(
        per_gb_second=DEFAULT_PRICE_PER_GB_SECOND[arch] if per_gb_second is None else per_gb_second,
        per_million_requests=(DEFAULT_PRICE_PER_MILLION_REQUESTS
                              if per_million_requests is None else per_million_requests),
        source="set on the command line" if custom else f"{arch} {DEFAULT_PRICE_SOURCE}",
    )
