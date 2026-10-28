"""What a call costs, approximately, in euros; unknown when the price is."""

import dataclasses

from odoo.tests.common import BaseCase

from .. import pricing
from ..connectors.base import ChatResponse, Usage
from ..models.cloud_settings import AssistantConfig


class TestPricing(BaseCase):
    """Known models by their longest prefix; listings for OpenRouter."""

    def test_a_known_anthropic_model_costs_its_tokens(self):
        """Input, output, cache write and cache read, each at its price."""
        prices = pricing.price_for("anthropic", "claude-opus-5-5", lambda _m: None)
        usage = Usage(
            1_000_000, 100_000, cache_read_tokens=1_000_000, cache_write_tokens=10_000
        )
        usd = 4.0 + 20.0 * 0.1 + 0.2 + 5.0 * 0.01
        self.assertAlmostEqual(
            pricing.cost_eur(prices, usage), round(usd * pricing.EUR_PER_USD, 6)
        )

    def test_a_dated_version_has_its_model_s_price_and_a_variant_none(self):
        """A snapshot is its model; ``claude-opus-5-7`` is not ``claude-opus-5``."""
        none = lambda _m: None  # the listing of a provider with a table
        self.assertEqual(
            pricing.price_for("anthropic", "claude-opus-5-5", none),
            pricing.ANTHROPIC["claude-opus-5-5"],
        )
        self.assertEqual(
            pricing.price_for("anthropic", "claude-haiku-4-5-20251001", none),
            pricing.ANTHROPIC["claude-haiku-4-5"],
        )
        self.assertIsNone(pricing.price_for("anthropic", "claude-opus-5-7", none))

    def test_no_model_has_no_price(self):
        """An answer that names no model is not priced as the table's first row."""
        self.assertIsNone(pricing.price_for("anthropic", "", lambda _m: None))

    def test_a_long_prompt_costs_what_the_provider_charges_past_its_threshold(self):
        """Claude Haiku 5.5 past 100,000 prompt tokens: the higher prices."""
        none = lambda _m: None  # the listing of a provider with a table
        short = pricing.price_for("anthropic", "claude-haiku-5-5", none, 90_000)
        long = pricing.price_for("anthropic", "claude-haiku-5-5", none, 120_000)
        self.assertEqual(short, pricing.ANTHROPIC["claude-haiku-5-5"])
        self.assertEqual(long[:2], (0.50, 2.50))

    def test_an_unknown_model_costs_nothing_known(self):
        """No price: None, never zero."""
        self.assertIsNone(
            pricing.cost_eur(
                pricing.price_for("anthropic", "claude-new", lambda _m: None),
                Usage(5, 5),
            )
        )

    def test_openrouter_prices_come_from_its_listing(self):
        """Dollars per million tokens, from the listing; a router's -1 is no price."""
        listing = {
            "m/x": {"id": "m/x", "price_in": 2.0, "price_out": 8.0},
            "m/auto": {"id": "m/auto", "price_in": -1, "price_out": -1},
        }
        prices = pricing.price_for("openrouter", "m/x", listing.get)
        self.assertEqual(prices, (2.0, 8.0, 2.0, 2.0))
        self.assertIsNone(pricing.price_for("openrouter", "m/auto", listing.get))

    def test_a_response_is_priced_at_the_model_that_answered(self):
        """Another model: its price. A dated version of the one asked: that one's."""
        response = ChatResponse(
            text="",
            tool_calls=(),
            usage=Usage(20, 20),
            model="claude-opus-4-8",
            stop="end",
        )
        self.assertAlmostEqual(
            pricing.response_cost("anthropic", response, lambda _m: None),
            pricing.cost_eur(pricing.ANTHROPIC["claude-opus-4-8"], Usage(20, 20)),
        )
        listing = {"m/x": {"id": "m/x", "price_in": 2.0, "price_out": 8.0}}
        dated = dataclasses.replace(response, model="m/x-20260801")
        self.assertAlmostEqual(
            pricing.response_cost("openrouter", dated, listing.get, "m/x"),
            pricing.cost_eur((2.0, 8.0, 2.0, 2.0), Usage(20, 20)),
        )

    def test_openai_cache_writes_cost_1_25_times_the_input_from_gpt_5_6(self):
        """GPT-5.6 on: written tokens at 1.25x the input, also past the threshold."""
        none = lambda _m: None  # the listing of a provider with a table
        for model, price_in, long_in in (
            ("gpt-5.6-terra", 2.0, 4.0),
            ("gpt-6-astra", 10.0, 20.0),
            ("gpt-6-luna", 0.10, 0.20),
        ):
            response = ChatResponse(
                text="",
                tool_calls=(),
                usage=Usage(0, 0, cache_write_tokens=100_000),
                model=model,
                stop="end",
            )
            self.assertAlmostEqual(
                pricing.response_cost("openai", response, none),
                round(price_in * 1.25 * 0.1 * pricing.EUR_PER_USD, 6),
            )
            long = dataclasses.replace(
                response, usage=Usage(0, 0, cache_write_tokens=300_000)
            )
            self.assertAlmostEqual(
                pricing.response_cost("openai", long, none),
                round(long_in * 1.25 * 0.3 * pricing.EUR_PER_USD, 6),
            )
        before = ChatResponse(
            text="",
            tool_calls=(),
            usage=Usage(0, 0, cache_write_tokens=100_000),
            model="gpt-5.5",
            stop="end",
        )
        self.assertAlmostEqual(
            pricing.response_cost("openai", before, none),
            round(5.0 * 0.1 * pricing.EUR_PER_USD, 6),
        )

    def test_with_a_monthly_limit_a_model_without_a_price_is_not_usable(self):
        """Allowed and priced: usable. Unpriced: only without a limit."""
        listing = (
            {"id": "claude-opus-5-5", "name": "Opus"},
            {"id": "claude-new-x", "name": "New"},
        )
        config = AssistantConfig(
            enabled=True,
            provider="anthropic",
            has_key=True,
            default_model="claude-opus-5-5",
            allowed_models=("claude-opus-5-5", "claude-new-x"),
            models=listing,
            user_ids=frozenset(),
            monthly_limit=0.0,
            step_cap=30,
        )
        self.assertTrue(pricing.model_usable(config, "claude-new-x"))
        self.assertFalse(pricing.model_usable(config, "claude-haiku-5-5"))
        capped = dataclasses.replace(config, monthly_limit=10.0)
        self.assertTrue(pricing.model_usable(capped, "claude-opus-5-5"))
        self.assertFalse(pricing.model_usable(capped, "claude-new-x"))
