"""The LLM gateway's metering: what can be priced, and for how much.

The proxy routes these once accompanied are gone -- callers hold the
provider key and stream directly -- so what is left to test is the part
that stayed here. Pricing is the reason it stayed: a caller that priced its
own calls could declare any number.
"""

from __future__ import annotations

import pytest

from orchestra.web.api.llm import views

# --------------------------------------------------------------------------- #
# Pure-helper unit tests
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Native-Anthropic leg
# --------------------------------------------------------------------------- #


class TestAnthropicPricing:
    """Anthropic bills in tokens and never in money, so we price it ourselves.

    That makes the rate table load-bearing in a way the OpenRouter leg's is
    not: there, a wrong number is a reporting error; here it is the charge.
    """

    def test_rates_come_from_the_curated_catalogue(self):
        assert views._anthropic_token_rates("claude-opus-5") == (5.0, 25.0)

    def test_the_endpoint_suffixed_form_resolves_too(self):
        """unillm speaks ``model@provider``; Anthropic is sent the bare id."""
        assert views._anthropic_token_rates("claude-opus-5@anthropic") == (5.0, 25.0)

    def test_an_unpriceable_model_resolves_to_nothing(self):
        assert views._anthropic_token_rates("gpt-4o") is None

    def test_cost_is_tokens_at_the_catalogue_rate(self):
        rates = (5.0, 25.0)
        cost = views._anthropic_usage_cost(
            {"input_tokens": 1000, "output_tokens": 500},
            rates,
        )
        assert cost == pytest.approx(1000 * 5.0 / 1e6 + 500 * 25.0 / 1e6)

    def test_cache_tokens_are_charged_rather_than_dropped(self):
        """Ignoring them would bill a cached call as though it were free."""
        rates = (5.0, 25.0)
        plain = views._anthropic_usage_cost({"input_tokens": 1000}, rates)
        cached = views._anthropic_usage_cost(
            {
                "input_tokens": 1000,
                "cache_read_input_tokens": 4000,
                "cache_creation_input_tokens": 1000,
            },
            rates,
        )
        assert cached > plain

    def test_usage_without_token_counts_prices_nothing(self):
        """Better no row than a zero-cost row implying the call was free."""
        assert views._anthropic_usage_cost({}, (5.0, 25.0)) is None


# --------------------------------------------------------------------------- #
# Metering API (used by the pod-local broker, which holds the provider key)
# --------------------------------------------------------------------------- #


class TestMeterability:
    """What can be charged decides what may be run.

    A caller streaming bytes itself can only be allowed to do so for a call
    this side could later price. Authorising something unpriceable would
    hand out inference nobody can bill.
    """

    def test_openrouter_models_are_always_meterable(self):
        """They report their own cost, so any of them can be settled."""
        assert views._is_meterable("openai/gpt-5.6-sol@openrouter") is True
        assert views._is_meterable("openrouter/openai/gpt-5.6-sol") is True

    def test_a_catalogued_anthropic_model_is_meterable(self):
        assert views._is_meterable("claude-opus-5@anthropic") is True

    def test_a_provider_shaped_bare_id_is_not(self):
        """A bare id names no leg, so nothing says how it would be priced.

        Callers must authorize in accounting form (``<id>@<provider>``): the
        provider-shaped spelling out of a request body matches the OpenRouter
        marker never, and the curated catalogue only by coincidence.
        """
        assert views._is_meterable("openai/gpt-5.4-mini") is False

    def test_an_uncatalogued_native_model_is_not(self):
        assert views._is_meterable("mystery-model@vertex-ai") is False


class TestPriceUsageForEitherProvider:
    """One entry point, because the caller reports and never computes.

    Leaving the pricing decision here is what keeps refusing an unpriceable
    model meaningful: a caller that priced its own calls could simply
    declare a number.
    """

    def test_an_openrouter_cost_is_taken_as_authoritative(self):
        assert views._price_usage_for("openai/x@openrouter", {"cost": 0.000225}) == (
            pytest.approx(0.000225)
        )

    def test_anthropic_tokens_are_priced_from_the_catalogue(self):
        cost = views._price_usage_for(
            "claude-opus-5",
            {"input_tokens": 1000, "output_tokens": 500},
        )
        assert cost == pytest.approx(0.0175)

    def test_usage_for_an_unpriceable_model_yields_nothing(self):
        """Better no charge than an invented one; authorize refuses these."""
        assert views._price_usage_for("mystery@vertex-ai", {"input_tokens": 10}) is None
