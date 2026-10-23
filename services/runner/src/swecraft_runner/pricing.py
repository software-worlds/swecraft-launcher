"""Configured fallback pricing for agents that report tokens but no cost."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelPricing:
    """USD rates per million tokens for one exact Harbor model identifier.

    Harbor reports cached tokens as a subset of input tokens. The cached rate
    defaults to the ordinary input rate when a provider has no separate cache
    price.
    """

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("input_per_million", self.input_per_million),
            ("output_per_million", self.output_per_million),
            ("cached_input_per_million", self.cached_input_per_million),
        ):
            if value is None:
                continue
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")

    def estimate(
        self, *, input_tokens: int, output_tokens: int, cache_tokens: int = 0
    ) -> float:
        """Estimate one run's cost from Harbor's aggregate token counters."""

        input_count = max(0, input_tokens)
        output_count = max(0, output_tokens)
        cached_count = min(input_count, max(0, cache_tokens))
        uncached_count = input_count - cached_count
        cached_rate = (
            self.input_per_million
            if self.cached_input_per_million is None
            else self.cached_input_per_million
        )
        return (
            uncached_count * self.input_per_million
            + cached_count * cached_rate
            + output_count * self.output_per_million
        ) / 1_000_000

    def as_dict(self) -> dict[str, float]:
        result = {
            "input_per_million": self.input_per_million,
            "output_per_million": self.output_per_million,
        }
        if self.cached_input_per_million is not None:
            result["cached_input_per_million"] = self.cached_input_per_million
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ModelPricing:
        allowed = {
            "input_per_million",
            "cached_input_per_million",
            "output_per_million",
        }
        unknown = set(value) - allowed
        if unknown:
            names = ", ".join(sorted(str(key) for key in unknown))
            raise ValueError(f"unknown pricing keys: {names}")
        missing = {"input_per_million", "output_per_million"} - set(value)
        if missing:
            raise ValueError("missing pricing keys: " + ", ".join(sorted(missing)))
        cached = value.get("cached_input_per_million")
        try:
            input_rate = float(value["input_per_million"])
            cached_rate = None if cached is None else float(cached)
            output_rate = float(value["output_per_million"])
        except (TypeError, ValueError) as error:
            raise ValueError("pricing rates must be numbers") from error
        return cls(
            input_per_million=input_rate,
            cached_input_per_million=cached_rate,
            output_per_million=output_rate,
        )


def agent_usage(
    agent_result: Mapping[str, Any], pricing: ModelPricing | None = None
) -> dict[str, int | float]:
    """Normalize Harbor counters and fill a missing cost from configured rates."""

    input_tokens = _counter(agent_result.get("n_input_tokens"))
    cache_tokens = _counter(agent_result.get("n_cache_tokens"))
    output_tokens = _counter(agent_result.get("n_output_tokens"))
    reported_cost = agent_result.get("cost_usd")
    if reported_cost is None and pricing is not None:
        cost = pricing.estimate(
            input_tokens=input_tokens,
            cache_tokens=cache_tokens,
            output_tokens=output_tokens,
        )
    else:
        try:
            cost = float(reported_cost or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
    return {
        "input_tokens": input_tokens,
        "cache_tokens": cache_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
    }


def _counter(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
