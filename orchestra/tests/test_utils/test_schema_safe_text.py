"""End-to-end checks that hardened request schemas reject HTML/XSS payloads.

These complement ``test_safe_text.py`` (which unit-tests the validator) by
constructing the *actual* Pydantic request models used across the API and
asserting that an injection payload in an identity/label/title/description
field is rejected with a ``ValidationError`` (surfaced by FastAPI as a 422).

Each case targets a single field; we assert the error is attributed to that
field so a future refactor that drops the validator fails loudly.
"""

import pytest
from pydantic import ValidationError

from orchestra.web.api.admin.schema import (
    BillingPlanTemplateCreate,
    PlanGroupCreateRequest,
)
from orchestra.web.api.assistant.schema import (
    AssistantUpdate,
    DemoAssistantCreate,
    VoiceCreate,
)
from orchestra.web.api.dashboard.schema import DashboardActionRecord
from orchestra.web.api.interface.schema import (
    UpdateInterfaceRequest,
    UpdateTabRequest,
    UpdateTileRequest,
)
from orchestra.web.api.plot.schema import UpdatePlotRequest
from orchestra.web.api.table_view.schema import UpdateTableViewRequest

# A representative tag-based payload (full incident payload lives in test_safe_text).
PAYLOAD = "<script>alert(document.domain)</script>"


# (field_name, factory) — factory injects PAYLOAD into ``field_name``.
SCHEMA_CASES = [
    ("first_name", lambda: AssistantUpdate(first_name=PAYLOAD)),
    ("surname", lambda: AssistantUpdate(surname=PAYLOAD)),
    ("about", lambda: AssistantUpdate(about=PAYLOAD)),
    ("job_title", lambda: AssistantUpdate(job_title=PAYLOAD)),
    ("label", lambda: DemoAssistantCreate(
        source_assistant_id=1,
        label=PAYLOAD,
        first_name="Lucy",
        surname="Demo",
        demoer_phone="+14155559999",
    )),
    ("name", lambda: VoiceCreate(
        voice_id="v1", name=PAYLOAD, description="ok", language="en",
    )),
    ("description", lambda: VoiceCreate(
        voice_id="v1", name="ok", description=PAYLOAD, language="en",
    )),
    ("name", lambda: UpdateTileRequest(name=PAYLOAD)),
    ("name", lambda: UpdateTabRequest(name=PAYLOAD)),
    ("name", lambda: UpdateInterfaceRequest(name=PAYLOAD)),
    ("title", lambda: UpdatePlotRequest(title=PAYLOAD)),
    ("title", lambda: UpdateTableViewRequest(title=PAYLOAD)),
    ("action_name", lambda: DashboardActionRecord(
        tile_token="t", action_name=PAYLOAD, function_id=1,
    )),
    ("label", lambda: DashboardActionRecord(
        tile_token="t", action_name="ok", function_id=1, label=PAYLOAD,
    )),
    ("name", lambda: PlanGroupCreateRequest(name=PAYLOAD)),
    ("display_name", lambda: PlanGroupCreateRequest(
        name="ok", display_name=PAYLOAD,
    )),
    ("description", lambda: PlanGroupCreateRequest(
        name="ok", description=PAYLOAD,
    )),
    ("name", lambda: BillingPlanTemplateCreate(
        name=PAYLOAD, billing_mode="CREDITS",
    )),
]


@pytest.mark.parametrize(
    "field, factory",
    SCHEMA_CASES,
    ids=[f"{i}:{field}" for i, (field, _) in enumerate(SCHEMA_CASES)],
)
def test_request_schemas_reject_xss(field, factory):
    with pytest.raises(ValidationError) as exc:
        factory()
    locs = {loc for err in exc.value.errors() for loc in err["loc"]}
    assert field in locs


def test_legitimate_values_still_accepted():
    # Sanity: the hardening does not reject ordinary, punctuation-rich input.
    assert AssistantUpdate(first_name="Ada", about="Mathematician & writer").first_name == "Ada"
    assert VoiceCreate(
        voice_id="v1",
        name="English Woman — Calm #1",
        description="Calm, relaxing voice (100% natural)",
        language="en",
    ).name == "English Woman — Calm #1"
    assert UpdateInterfaceRequest(name="Q3 O'Brien Dashboard").name == "Q3 O'Brien Dashboard"
