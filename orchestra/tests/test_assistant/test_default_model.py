from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS
from orchestra.web.api.assistant.default_models import (
    DEFAULT_MODEL_OPTIONS,
    PLATFORM_DEFAULT_DISPLAY_NAME,
    PLATFORM_DEFAULT_MODEL,
    PLATFORM_SLOW_BRAIN_DISPLAY_NAME,
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
    assert PLATFORM_DEFAULT_DISPLAY_NAME in options[0]["label"]
    assert options[0]["approx_credits_per_task"] == next(
        o.approx_credits_per_task
        for o in DEFAULT_MODEL_OPTIONS
        if o.model == PLATFORM_DEFAULT_MODEL and o.reasoning_effort == "high"
    )
    assert options[1]["model"] == "minimax-v3@minimax"
    assert options[1]["label"] == "MiniMax-M3"
    pairs = {(o["model"], o["reasoning_effort"]) for o in options}
    assert (None, None) in pairs
    assert ("minimax-v3@minimax", None) in pairs
    assert ("kimi-k3@moonshotai", None) in pairs
    assert (PLATFORM_DEFAULT_MODEL, "high") in pairs
    assert ("openai/gpt-5.6-sol@openrouter", "high") in pairs
    assert ("openai/gpt-5.6-terra@openrouter", "medium") in pairs
    assert ("openai/gpt-5.6-luna@openrouter", "low") in pairs
    assert ("claude-4.8-opus@anthropic", "medium") in pairs
    assert ("claude-opus-5@anthropic", "high") in pairs
    assert ("claude-fable-5@anthropic", "low") in pairs
    assert ("claude-sonnet-5@anthropic", "high") in pairs
    assert ("gemini-3-pro@vertex-ai", "medium") in pairs
    assert all(o["label"] for o in options)
    assert all(o["approx_credits_per_task"] > 0 for o in options)
    assert all(o["approx_credits_per_message"] > 0 for o in options)
    assert all(
        o["artificial_analysis_url"].startswith("https://artificialanalysis.ai/models/")
        for o in options
    )


@pytest.mark.anyio
async def test_list_slow_brain_model_options(client: AsyncClient):
    resp = await client.get(
        "/v0/assistant/default-model-options",
        params={"usage": "slow_brain"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    options = resp.json()["info"]
    assert options[0]["model"] is None
    assert PLATFORM_SLOW_BRAIN_DISPLAY_NAME in options[0]["label"]
    assert options[0]["approx_credits_per_message"] == next(
        o.approx_credits_per_message
        for o in DEFAULT_MODEL_OPTIONS
        if o.model == "openai/gpt-5.6-terra@openrouter" and o.reasoning_effort == "high"
    )
    # Selectable pairs match the actor catalog (minus the system-default row).
    actor = await client.get("/v0/assistant/default-model-options", headers=HEADERS)
    assert {(o["model"], o["reasoning_effort"]) for o in options[1:]} == {
        (o["model"], o["reasoning_effort"]) for o in actor.json()["info"][1:]
    }


@pytest.mark.anyio
async def test_default_model_options_costs_rank_sensibly(client: AsyncClient):
    """Within a concrete model, higher effort costs more."""
    resp = await client.get("/v0/assistant/default-model-options", headers=HEADERS)
    options = resp.json()["info"]
    by_model: dict = {}
    for o in options:
        if o["model"] is None:
            continue
        by_model.setdefault(o["model"], []).append(o["approx_credits_per_task"])
    for model, costs in by_model.items():
        assert costs == sorted(costs), model
    # System default mirrors the platform Sol-high task estimate.
    assert options[0]["approx_credits_per_task"] == next(
        o["approx_credits_per_task"]
        for o in options
        if o["model"] == PLATFORM_DEFAULT_MODEL and o["reasoning_effort"] == "high"
    )


@pytest.mark.anyio
async def test_message_credits_rank_sensibly(client: AsyncClient):
    """Per-message credits rise with effort; Terra high > Luna high (token rates)."""
    resp = await client.get("/v0/assistant/default-model-options", headers=HEADERS)
    options = resp.json()["info"]
    by_model: dict = {}
    for o in options:
        if o["model"] is None:
            continue
        by_model.setdefault(o["model"], []).append(o["approx_credits_per_message"])
    for model, costs in by_model.items():
        assert costs == sorted(costs), model
    terra_high = next(
        o["approx_credits_per_message"]
        for o in options
        if o["model"] == "openai/gpt-5.6-terra@openrouter"
        and o["reasoning_effort"] == "high"
    )
    luna_high = next(
        o["approx_credits_per_message"]
        for o in options
        if o["model"] == "openai/gpt-5.6-luna@openrouter"
        and o["reasoning_effort"] == "high"
    )
    assert terra_high > luna_high


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
    assert info["slow_brain_model"] is None
    assert info["slow_brain_reasoning_effort"] is None


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
            "default_model": "openai/gpt-5.6-sol@openrouter",
            "default_reasoning_effort": "medium",
            "create_infra": False,
        },
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()["info"]
    assert updated["default_model"] == "openai/gpt-5.6-sol@openrouter"
    assert updated["default_reasoning_effort"] == "medium"
    # Changing the default model is a runtime-facing update.
    _, mock_reawaken = mock_assistant_infra_calls
    mock_reawaken.assert_awaited_once()


@pytest.mark.anyio
async def test_update_slow_brain_model(client: AsyncClient, mock_assistant_infra_calls):
    aid = await _create_assistant(client)
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={
            "slow_brain_model": "openai/gpt-5.6-luna@openrouter",
            "slow_brain_reasoning_effort": "medium",
            "create_infra": False,
        },
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()["info"]
    assert updated["slow_brain_model"] == "openai/gpt-5.6-luna@openrouter"
    assert updated["slow_brain_reasoning_effort"] == "medium"
    _, mock_reawaken = mock_assistant_infra_calls
    mock_reawaken.assert_awaited_once()


@pytest.mark.anyio
async def test_update_default_model_minimax_without_effort(client: AsyncClient):
    aid = await _create_assistant(client)
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={
            "default_model": "minimax-v3@minimax",
            "default_reasoning_effort": None,
            "create_infra": False,
        },
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()["info"]
    assert updated["default_model"] == "minimax-v3@minimax"
    assert updated["default_reasoning_effort"] is None


@pytest.mark.anyio
async def test_update_default_model_invalid_pair(client: AsyncClient):
    aid = await _create_assistant(client)
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={
            "default_model": "openai/gpt-5.6-sol@openrouter",
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
        default_model="openai/gpt-5.6-sol@openrouter",
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


@pytest.mark.anyio
async def test_clear_slow_brain_model(client: AsyncClient):
    aid = await _create_assistant(
        client,
        slow_brain_model="openai/gpt-5.6-sol@openrouter",
        slow_brain_reasoning_effort="high",
    )
    patch_resp = await client.patch(
        f"/v0/assistant/{aid}/config",
        json={"slow_brain_model": None, "create_infra": False},
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()["info"]
    assert updated["slow_brain_model"] is None
    assert updated["slow_brain_reasoning_effort"] is None
