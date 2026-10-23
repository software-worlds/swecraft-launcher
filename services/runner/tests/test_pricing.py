from __future__ import annotations

import unittest

from swecraft_runner.pricing import ModelPricing, agent_usage


class ModelPricingTests(unittest.TestCase):
    def test_estimates_uncached_cached_and_output_tokens(self) -> None:
        pricing = ModelPricing(
            input_per_million=0.95,
            cached_input_per_million=0.19,
            output_per_million=4.0,
        )

        cost = pricing.estimate(
            input_tokens=61_073,
            cache_tokens=33_472,
            output_tokens=255,
        )

        self.assertAlmostEqual(cost, 0.03360063)

    def test_cached_tokens_default_to_the_input_rate(self) -> None:
        pricing = ModelPricing(input_per_million=2.0, output_per_million=3.0)

        self.assertAlmostEqual(
            pricing.estimate(
                input_tokens=1_000_000,
                cache_tokens=400_000,
                output_tokens=1_000_000,
            ),
            5.0,
        )

    def test_provider_cost_takes_precedence_over_configured_pricing(self) -> None:
        pricing = ModelPricing(input_per_million=100.0, output_per_million=100.0)

        usage = agent_usage(
            {
                "n_input_tokens": 1_000_000,
                "n_cache_tokens": 250_000,
                "n_output_tokens": 1_000_000,
                "cost_usd": 0.25,
            },
            pricing,
        )

        self.assertEqual(usage["cache_tokens"], 250_000)
        self.assertEqual(usage["cost_usd"], 0.25)

    def test_missing_provider_cost_uses_configured_pricing(self) -> None:
        pricing = ModelPricing(input_per_million=1.0, output_per_million=2.0)

        usage = agent_usage(
            {
                "n_input_tokens": 1_000_000,
                "n_output_tokens": 500_000,
                "cost_usd": None,
            },
            pricing,
        )

        self.assertEqual(usage["cost_usd"], 2.0)

    def test_rejects_invalid_pricing(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite non-negative"):
            ModelPricing(input_per_million=-1.0, output_per_million=1.0)
        with self.assertRaisesRegex(ValueError, "unknown pricing keys"):
            ModelPricing.from_mapping(
                {
                    "input_per_million": 1,
                    "output_per_million": 2,
                    "surprise": 3,
                }
            )
        with self.assertRaisesRegex(ValueError, "missing pricing keys"):
            ModelPricing.from_mapping({"input_per_million": 1})


if __name__ == "__main__":
    unittest.main()
