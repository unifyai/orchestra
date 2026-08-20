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


# --------------------------------------------------------------------------- #
# Token-count normalization (the settle body's usage shape varies by provider)
# --------------------------------------------------------------------------- #


class TestTokenCountsFromUsage:
    """The scalars ``_charge_usage`` writes to ``detail``, from either provider shape."""

    def test_openai_compatible_scalars(self):
        prompt, completion, cached = views._token_counts_from_usage(
            {"prompt_tokens": 100, "completion_tokens": 20},
        )
        assert (prompt, completion, cached) == (100, 20, None)

    def test_cached_tokens_hoisted_by_the_broker_stream_merge(self):
        """The broker already lifts this to a top-level scalar on the stream path."""
        prompt, completion, cached = views._token_counts_from_usage(
            {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 64},
        )
        assert cached == 64

    def test_cached_tokens_still_nested_on_the_non_stream_path(self):
        """The body path forwards ``prompt_tokens_details`` verbatim, unlifted."""
        prompt, completion, cached = views._token_counts_from_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        )
        assert (prompt, completion, cached) == (100, 20, 30)

    def test_anthropic_shape_falls_back_to_input_output_tokens(self):
        prompt, completion, cached = views._token_counts_from_usage(
            {"input_tokens": 900, "output_tokens": 120},
        )
        assert (prompt, completion, cached) == (900, 120, None)

    def test_anthropic_cache_read_tokens_count_as_cached(self):
        prompt, completion, cached = views._token_counts_from_usage(
            {
                "input_tokens": 900,
                "output_tokens": 120,
                "cache_read_input_tokens": 400,
            },
        )
        assert cached == 400

    def test_a_non_dict_usage_yields_nothing(self):
        assert views._token_counts_from_usage(None) == (None, None, None)

    def test_an_empty_usage_yields_nothing(self):
        assert views._token_counts_from_usage({}) == (None, None, None)


# --------------------------------------------------------------------------- #
# Settle persistence: what actually lands in ``credit_transaction.detail``
# --------------------------------------------------------------------------- #


class _FakeSession:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _FakeBillingAccountDAO:
    """Stands in for the real DAO so persistence can be checked without a DB."""

    def __init__(self, session):
        self.session = session

    def deduct_credits(self, billing_account_id, quantity, **kwargs):
        self.session.deduct_calls.append(
            {"billing_account_id": billing_account_id, "quantity": quantity, **kwargs},
        )


def _charge(monkeypatch, **kwargs):
    """Drive ``_charge_usage`` against the fake DAO and return the recorded call."""
    monkeypatch.setattr(views, "BillingAccountDAO", _FakeBillingAccountDAO)

    session = _FakeSession()
    session.deduct_calls = []

    views._charge_usage(
        lambda: session,
        ctx=views._BillingCtx(billing_account_id=99, charges=True),
        user_id="user-1",
        organization_id=None,
        assistant_id=42,
        model="openai/gpt-5.6-sol@openrouter",
        raw_cost=0.01,
        **kwargs,
    )
    assert session.committed is True
    assert session.deduct_calls, "deduct_credits was never called"
    return session.deduct_calls[0]


class TestSettlePersistsUsageDetail:
    """The new observability keys must actually reach ``credit_transaction.detail``.

    Additive only: existing keys (``model``, ``raw_cost``, ``markup``) still
    have to be there, since old dashboards read them and rows predating this
    change never had the new keys at all.
    """

    def test_generation_id_is_persisted(self, monkeypatch):
        call = _charge(monkeypatch, generation_id="gen-abc123")
        assert call["detail"]["generation_id"] == "gen-abc123"

    def test_a_missing_generation_id_is_not_invented(self, monkeypatch):
        call = _charge(monkeypatch)
        assert "generation_id" not in call["detail"]

    def test_token_counts_are_persisted(self, monkeypatch):
        call = _charge(
            monkeypatch,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        )
        detail = call["detail"]
        assert detail["prompt_tokens"] == 100
        assert detail["completion_tokens"] == 20
        assert detail["cached_tokens"] == 30

    def test_a_label_carrying_billing_context_is_persisted(self, monkeypatch):
        call = _charge(monkeypatch, label="Researching leads")
        assert call["detail"]["label"] == "Researching leads"

    def test_a_chinese_label_is_persisted_intact(self, monkeypatch):
        label = "研究客户的 NotebookLM 连接选项"
        call = _charge(monkeypatch, label=label)
        assert call["detail"]["label"] == label

    def test_a_sandbox_call_with_no_label_gets_none_plus_a_source_tag(
        self,
        monkeypatch,
    ):
        """No billing context: no label, but the row still says where it came from."""
        call = _charge(monkeypatch)
        assert "label" not in call["detail"]
        assert call["detail"]["source"] == "gateway"

    def test_an_action_source_overrides_the_gateway_tag(self, monkeypatch):
        """A call carrying billing context reports its real action source."""
        call = _charge(monkeypatch, source="chat")
        assert call["detail"]["source"] == "chat"

    def test_existing_keys_are_preserved_alongside_the_new_ones(self, monkeypatch):
        call = _charge(monkeypatch, generation_id="gen-1", label="x", source="tool")
        detail = call["detail"]
        assert detail["model"] == "openai/gpt-5.6-sol@openrouter"
        assert detail["raw_cost"] == pytest.approx(0.01)
        assert detail["markup"] == pytest.approx(1.2)

    def test_all_new_keys_together(self, monkeypatch):
        call = _charge(
            monkeypatch,
            generation_id="gen-xyz",
            usage={
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "cached_tokens": 5,
            },
            label="客户研究",
            source="chat",
        )
        detail = call["detail"]
        assert detail["generation_id"] == "gen-xyz"
        assert detail["prompt_tokens"] == 50
        assert detail["completion_tokens"] == 10
        assert detail["cached_tokens"] == 5
        assert detail["label"] == "客户研究"
        assert detail["source"] == "chat"
