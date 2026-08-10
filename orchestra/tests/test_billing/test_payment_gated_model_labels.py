"""The catalogue must label exactly what the spend boundary will refuse.

These pin the prediction against the rule it mirrors in
``unify.spending_limits``. A picker that disagrees with enforcement is
worse than one that says nothing: too strict and it withholds a model
that would have worked, too loose and it promises one that gets refused
after the user has chosen it.
"""

import pytest

from orchestra.lib.payment_gated_models import (
    payment_gate_reason,
    provider_label,
    provider_of,
    vendor_of,
)


class TestWhatGetsLabelled:
    def test_a_gated_provider_is_refused_for_a_never_paid_account(self):
        reason = payment_gate_reason("claude-fable-5@anthropic", never_paid=True)

        assert reason is not None
        assert "Anthropic" in reason

    def test_nothing_is_labelled_once_the_account_has_paid(self):
        assert payment_gate_reason("claude-fable-5@anthropic", never_paid=False) is None

    def test_the_included_models_stay_selectable_while_unpaid(self):
        """Gating the platform default would leave a trial with nothing to run."""
        assert (
            payment_gate_reason("openai/gpt-5.6-sol@openrouter", never_paid=True)
            is None
        )

    def test_the_system_default_row_carries_no_model_and_must_not_crash(self):
        """The first catalogue row has ``model=None`` — it routes by default."""
        assert payment_gate_reason(None, never_paid=True) is None

    def test_a_bare_model_name_names_no_provider(self):
        assert payment_gate_reason("some-model", never_paid=True) is None

    def test_the_reason_states_the_condition_rather_than_a_remedy(self):
        """One line under a greyed row. The remedy belongs in the refusal."""
        reason = payment_gate_reason("claude-fable-5@anthropic", never_paid=True)

        assert len(reason) < 80
        assert "\n" not in reason


class TestProviderResolution:
    def test_the_provider_is_the_segment_after_the_last_at(self):
        assert provider_of("openai/gpt-5.6-terra@openrouter") == "openrouter"
        assert provider_of("claude-fable-5@anthropic") == "anthropic"

    def test_an_openrouter_routed_anthropic_model_is_still_gated(self):
        """The route is OpenRouter; the vendor is Anthropic, and that decides.

        The curated catalogue offers Anthropic natively while model search
        offers the same vendor through the aggregator, so matching only the
        route would leave the search path advertising exactly the models the
        gate exists to hold back. Mirrors ``_gated_provider_of`` in
        ``unify.spending_limits``.
        """
        assert provider_of("anthropic/claude-opus-4.8@openrouter") == "openrouter"
        assert vendor_of("anthropic/claude-opus-4.8@openrouter") == "anthropic"

        reason = payment_gate_reason(
            "anthropic/claude-opus-4.8@openrouter",
            never_paid=True,
        )
        assert reason is not None
        # Names the vendor being refused, not the aggregator it was routed by.
        assert "Anthropic" in reason
        assert "OpenRouter" not in reason

    def test_the_platform_default_survives_vendor_matching(self):
        """It shares the aggregator with the gated vendor and must not be caught."""
        assert vendor_of("openai/gpt-5.6-sol@openrouter") == "openai"
        assert (
            payment_gate_reason("openai/gpt-5.6-sol@openrouter", never_paid=True)
            is None
        )

    def test_providers_are_spelled_the_way_a_reader_writes_them(self):
        assert provider_label("anthropic") == "Anthropic"
        assert provider_label("openai") == "OpenAI"

    def test_an_unmapped_provider_falls_back_to_its_slug(self):
        assert provider_label("someprovider") == "someprovider"


class TestConfiguredProviderSet:
    def test_the_gated_set_is_deployment_tunable(self, monkeypatch):
        from orchestra.settings import settings

        monkeypatch.setattr(settings, "payment_gated_providers", "moonshotai")

        assert payment_gate_reason("kimi-k3@moonshotai", never_paid=True) is not None
        assert payment_gate_reason("claude-fable-5@anthropic", never_paid=True) is None

    def test_an_empty_setting_gates_nothing(self, monkeypatch):
        from orchestra.settings import settings

        monkeypatch.setattr(settings, "payment_gated_providers", "")

        assert payment_gate_reason("claude-fable-5@anthropic", never_paid=True) is None

    @pytest.mark.parametrize("raw", ["Anthropic", " anthropic ", "anthropic,openai"])
    def test_the_setting_tolerates_spacing_and_case(self, monkeypatch, raw):
        from orchestra.settings import settings

        monkeypatch.setattr(settings, "payment_gated_providers", raw)

        assert (
            payment_gate_reason("claude-fable-5@anthropic", never_paid=True) is not None
        )


class TestTheCatalogueEndpointAppliesTheLabels:
    """Exercises the real curated catalogue, without a database.

    The endpoint's own integration tests need Postgres; these cover the
    part that is easy to get wrong -- feeding every catalogue row through
    the predicate, including the system-default row whose model is
    ``None``.
    """

    @staticmethod
    def _options(never_paid: bool):
        from unittest.mock import patch

        from fastapi import Request

        from orchestra.web.api.assistant.views import list_default_model_options

        scope = {"type": "http", "headers": []}
        request = Request(scope)
        request.state.user_id = "user-1"
        request.state.organization_id = None

        with patch(
            "orchestra.lib.payment_gated_models.account_never_paid",
            return_value=never_paid,
        ):
            return list_default_model_options(
                request,
                usage="actor",
                session=None,
            ).info

    def test_the_system_default_row_survives_a_never_paid_account(self):
        """Its model is None; a predicate that assumed a string would 500."""
        options = self._options(never_paid=True)

        default_row = next(o for o in options if o.model is None)
        assert default_row.eligible is True
        assert default_row.disabled_reason is None

    def test_gated_rows_are_marked_and_carry_a_reason(self):
        options = self._options(never_paid=True)

        gated = [o for o in options if o.model and o.model.endswith("@anthropic")]
        assert gated, "the curated catalogue should still offer Anthropic models"
        for option in gated:
            assert option.eligible is False
            assert option.disabled_reason
            assert "Anthropic" in option.disabled_reason

    def test_a_paid_account_sees_the_whole_catalogue_selectable(self):
        options = self._options(never_paid=False)

        assert all(o.eligible for o in options)
        assert all(o.disabled_reason is None for o in options)

    def test_included_models_stay_selectable_for_a_never_paid_account(self):
        options = self._options(never_paid=True)

        openrouter = [o for o in options if o.model and o.model.endswith("@openrouter")]
        assert openrouter
        assert all(o.eligible for o in openrouter)
