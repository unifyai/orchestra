"""The opening config is stored in the casing the runtime reads it by.

This object is persisted verbatim on the call session and dispatched verbatim to
the assistant runtime, which looks its fields up by snake_case name. A client
forwarding its own camelCase spelling wrote a config that travelled the whole way
and then could not be read: the asset naming what to play was absent, so the
voice agent refused the opening and the caller heard nothing. Normalising at the
API boundary is what stops that depending on every client getting it right.
"""

from __future__ import annotations

import pytest

from orchestra.web.api.calls.schema import (
    AssistantCallCreate,
    CallCreate,
    OwnedAssistantCallCreate,
    normalise_opening_config,
)

_RECORDED_CAMEL = {
    "mode": "recorded",
    "recordingAsset": "coordinator_onboarding_intro",
    "source": "coordinator_onboarding_intro",
}


class TestTheBoundaryConverts:
    def test_a_camel_cased_asset_becomes_readable(self):
        converted = normalise_opening_config(_RECORDED_CAMEL)

        assert converted["recording_asset"] == "coordinator_onboarding_intro"
        assert "recordingAsset" not in converted

    def test_every_recognised_field_is_converted(self):
        converted = normalise_opening_config(
            {
                "openerText": "hello",
                "simulatedUtterance": "hi",
                "recordingPath": "/tmp/a.wav",
                "recordingUrl": "https://example.com/a.wav",
            },
        )

        assert set(converted) == {
            "opener_text",
            "simulated_utterance",
            "recording_path",
            "recording_url",
        }

    def test_an_already_converted_config_is_unchanged(self):
        original = {
            "mode": "recorded",
            "recording_asset": "coordinator_onboarding_intro",
        }

        assert normalise_opening_config(original) == original

    def test_an_unrecognised_field_is_left_alone(self):
        converted = normalise_opening_config({"mode": "speak", "someOtherThing": 1})

        assert converted == {"mode": "speak", "someOtherThing": 1}

    @pytest.mark.parametrize("value", [None, "", 7])
    def test_a_non_object_passes_through(self, value):
        assert normalise_opening_config(value) == value


class TestEveryCreateRouteNormalises:
    """All three request bodies can carry the config, so all three convert it."""

    def test_scope_create(self):
        body = CallCreate(
            kind="assistant_dm",
            assistant_id=2310,
            opening_config=dict(_RECORDED_CAMEL),
        )

        assert body.opening_config["recording_asset"] == "coordinator_onboarding_intro"

    def test_assistant_initiated_ring(self):
        body = AssistantCallCreate(
            assistant_id=2310,
            opening_config=dict(_RECORDED_CAMEL),
        )

        assert body.opening_config["recording_asset"] == "coordinator_onboarding_intro"

    def test_owner_scoped_ring(self):
        body = OwnedAssistantCallCreate(opening_config=dict(_RECORDED_CAMEL))

        assert body.opening_config["recording_asset"] == "coordinator_onboarding_intro"

    def test_a_call_without_an_opening_is_untouched(self):
        assert CallCreate(kind="dm", peer_user_id="u1").opening_config is None
