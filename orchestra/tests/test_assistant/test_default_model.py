from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS
from orchestra.web.api.assistant.default_models import (
    DEFAULT_MODEL_OPTIONS,
    PLATFORM_DEFAULT_MODEL,
)


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls():
    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        yield mock_wake_up, mock_reawaken


async def _create_assistant(client: AsyncClient, **extra) -> int:
    payload = {
        "first_name": "Modela",
        "surname": "Tester",
        "create_infra": False,
        **extra,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create.status_code == 200, create.text
    return create.json()["info"]["agent_id"]


@pytest.mark.anyio
async def test_list_default_model_options(client: AsyncClient):
    resp = await client.get("/v0/assistant/default-model-options", headers=HEADERS)
    assert resp.status_code == 200
    options = resp.json()["info"]
    assert len(options) == len(DEFAULT_MODEL_OPTIONS)
    assert options[0]["model"] is None
    assert options[0]["reasoning_effort"] is None
    assert "System Default" in options[0]["label"]
    assert options[1]["model"] == PLATFORM_DEFAULT_MODEL
    assert options[1]["label"] == "MiniMax-M3"
    pairs = {(o["model"], o["reasoning_effort"]) for o in options}
    assert (None, None) in pairs
    assert (PLATFORM_DEFAULT_MODEL, None) in pairs
    assert ("gpt-5.6-sol@openai", "high") in pairs
    assert ("gpt-5.6-terra@openai", "medium") in pairs
    assert ("gpt-5.6-luna@openai", "low") in pairs
    assert ("claude-4.8-opus@anthropic", "medium") in pairs
    assert ("claude-fable-5@anthropic", "low") in pairs
    assert ("claude-sonnet-5@anthropic", "high") in pairs
    assert ("gemini-3-pro@vertex-ai", "medium") in pairs
    assert all(o["label"] for o in options)
    assert all(o["approx_credits_per_task"] > 0 for o in options)
    assert all(
        o["artificial_analysis_url"].startswith("https://artificialanalysis.ai/models/")
        for o in options
    )


@pytest.mark.anyio
async def test_default_model_options_costs_rank_sensibly(client: AsyncClient):
    """Within a concrete model, higher effort costs more; system default is cheapest."""
    resp = await client.get("/v0/assistant/default-model-options", headers=HEADERS)
    options = resp.json()["info"]
    by_model: dict = {}
    for o in options:
        if o["model"] is None:
            continue
        by_model.setdefault(o["model"], []).append(o["approx_credits_per_task"])
    for model, costs in by_model.items():
        assert costs == sorted(costs), model
    assert options[0]["approx_credits_per_task"] == min(
        o["approx_credits_per_task"] for o in options
    )


@pytest.mark.anyio
async def test_default_model_defaults_to_null(client: AsyncClient):
    aid = await _create_assistant(client)
    listing = await client.get(
        "/v0/assistant",
        params={"agent_id": aid},
        headers=HEADERS,
    )
    info = listing.json()["info"][0]
    assert info["default_model"] is None
    assert info["default_reasoning_effort"] is None


@pytest.mark.anyio
async def test_create_assistant_with_default_model(client: AsyncClient):
    aid = await _create_assistant(
        client,
        default_model="claude-fable-5@anthropic",
        default_reasoning_effort="high",
    )
    listing = await client.get(
        "/v0/assistant",
        params={"agent_id": aid},
        headers=HEADERS,
    )
    info = listing.json()["info"][0]
    assert info["default_model"] == "claude-fable-5@anthropic"
    assert info["default_reasoning_effort"] == "high"


@pytest.mark.anyio
async def test_create_assistant_with_invalid_default_model(client: AsyncClient):
    payload = {
        "first_name": "Modela",
        "surname": "Tester",
        "create_infra": False,
        "default_model": "deepseek-v4-max@deepseek",
        "default_reasoning_effort": "high",
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_update_default_model(client: AsyncClient, mock_assistant_infra_calls):
    aid = await _create_assistant(client)
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={
            "default_model": "gpt-5.6-sol@openai",
            "default_reasoning_effort": "medium",
            "create_infra": False,
        },
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()["info"]
    assert updated["default_model"] == "gpt-5.6-sol@openai"
    assert updated["default_reasoning_effort"] == "medium"
    # Changing the default model is a runtime-facing update.
    _, mock_reawaken = mock_assistant_infra_calls
    mock_reawaken.assert_awaited_once()


@pytest.mark.anyio
async def test_update_default_model_minimax_without_effort(client: AsyncClient):
    aid = await _create_assistant(client)
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={
            "default_model": PLATFORM_DEFAULT_MODEL,
            "default_reasoning_effort": None,
            "create_infra": False,
        },
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()["info"]
    assert updated["default_model"] == PLATFORM_DEFAULT_MODEL
    assert updated["default_reasoning_effort"] is None


@pytest.mark.anyio
async def test_update_default_model_invalid_pair(client: AsyncClient):
    aid = await _create_assistant(client)
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={
            "default_model": "gpt-5.6-sol@openai",
            "default_reasoning_effort": "max",
            "create_infra": False,
        },
        headers=HEADERS,
    )
    assert patch_resp.status_code == 422


@pytest.mark.anyio
async def test_update_effort_without_model_rejected(client: AsyncClient):
    aid = await _create_assistant(client)
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={"default_reasoning_effort": "high", "create_infra": False},
        headers=HEADERS,
    )
    assert patch_resp.status_code == 422


@pytest.mark.anyio
async def test_clear_default_model(client: AsyncClient):
    aid = await _create_assistant(
        client,
        default_model="gpt-5.6-sol@openai",
        default_reasoning_effort="high",
    )
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={"default_model": None, "create_infra": False},
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()["info"]
    assert updated["default_model"] is None
    assert updated["default_reasoning_effort"] is None
