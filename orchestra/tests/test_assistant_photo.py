from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.services.replicate_service import ReplicateAPIError
from orchestra.services.replicate_service import (
    ReplicateService as OriginalReplicateService,
)
from orchestra.tests.utils import HEADERS


@pytest.fixture(autouse=True)
def mock_services_factory(fastapi_app):
    """
    Mocks ReplicateService and patches Pub/Sub to prevent middleware errors.
    This follows the simplified testing strategy of assuming credit checks are
    bypassed (i.e., running in a staging-like mode).
    """
    replicate_mock = MagicMock(spec=OriginalReplicateService)
    replicate_mock.generate_photo.return_value = (
        "https://replicate.example.com/generated.jpg"
    )
    replicate_mock.edit_photo.return_value = "https://replicate.example.com/edited.jpg"

    # Patch Pub/Sub to prevent middleware from failing on unserializable mock objects
    with patch(
        "orchestra.web.api.utils.production_traffic_middleware.send_pubsub_msg",
    ):
        # Override the ReplicateService dependency in the FastAPI app
        fastapi_app.dependency_overrides[
            OriginalReplicateService
        ] = lambda: replicate_mock

        yield replicate_mock

        # Clean up the override after the test
        fastapi_app.dependency_overrides.pop(OriginalReplicateService, None)


@pytest.mark.anyio
async def test_generate_photo_success(client: AsyncClient, mock_services_factory):
    """Test successful photo generation."""
    replicate_mock = mock_services_factory
    user_id = "test-user-generate-ok"

    payload = {
        "prompt": "A successful test prompt",
        "aspect_ratio": "16:9",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/photo/generate",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 201
    data = resp.json()["info"]
    assert data["url"] == "https://replicate.example.com/generated.jpg"

    # Verify Replicate service was called correctly with provided and default values
    replicate_mock.generate_photo.assert_called_once_with(
        prompt=payload["prompt"],
        aspect_ratio="16:9",  # Check that non-default value is passed
        output_format="webp",
        output_quality=80,
        safety_tolerance=2.0,
        prompt_upsampling=True,
    )


@pytest.mark.anyio
async def test_generate_photo_replicate_fails(
    client: AsyncClient,
    mock_services_factory,
):
    """Test photo generation when the Replicate service returns an error."""
    replicate_mock = mock_services_factory
    user_id = "test-user-generate-fail-api"

    replicate_mock.generate_photo.side_effect = ReplicateAPIError(
        status_code=503,
        detail="Replicate is down",
    )

    payload = {"prompt": "A prompt that triggers an API error"}
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/photo/generate",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 503
    assert "Replicate API error: Replicate is down" in resp.json()["detail"]


@pytest.mark.anyio
async def test_edit_photo_success(client: AsyncClient, mock_services_factory):
    """Test successful photo editing."""
    replicate_mock = mock_services_factory
    user_id = "test-user-edit-ok"

    payload = {
        "prompt": "Make it a cubist painting",
        "input_image": "http://example.com/image.jpg",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/photo/edit",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 201
    data = resp.json()["info"]
    assert data["url"] == "https://replicate.example.com/edited.jpg"

    # Verify Replicate service was called correctly
    replicate_mock.edit_photo.assert_called_once_with(
        prompt=payload["prompt"],
        input_image=payload["input_image"],
        aspect_ratio="match_input_image",
        output_format="jpg",
        safety_tolerance=2.0,
    )


@pytest.mark.anyio
async def test_edit_photo_replicate_fails(client: AsyncClient, mock_services_factory):
    """Test photo editing when the Replicate service returns an error."""
    replicate_mock = mock_services_factory
    user_id = "test-user-edit-fail-api"

    replicate_mock.edit_photo.side_effect = ReplicateAPIError(
        status_code=500,
        detail="Replicate edit model failed",
    )

    payload = {
        "prompt": "An edit that fails",
        "input_image": "http://example.com/image.jpg",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/photo/edit",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 500
    assert "Replicate API error: Replicate edit model failed" in resp.json()["detail"]
