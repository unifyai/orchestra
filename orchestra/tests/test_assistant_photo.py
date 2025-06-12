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
    Mocks ReplicateService (via dependency injection) and UsersDAO (via patching).
    The mocks are automatically applied to every test in this module.
    Yields mock instances for test-specific configuration.
    Also patches send_pubsub_msg to prevent middleware errors.
    """
    replicate_mock = MagicMock(spec=OriginalReplicateService)
    replicate_mock.generate_photo.return_value = (
        "https://replicate.example.com/generated.jpg"
    )
    replicate_mock.edit_photo.return_value = "https://replicate.example.com/edited.jpg"

    with patch("orchestra.web.api.assistant.views.UsersDAO") as MockUsersDAO, patch(
        "orchestra.web.api.utils.production_traffic_middleware.send_pubsub_msg",
    ):
        users_dao_mock = MockUsersDAO.return_value

        fastapi_app.dependency_overrides[
            OriginalReplicateService
        ] = lambda: replicate_mock

        yield replicate_mock, users_dao_mock

        fastapi_app.dependency_overrides.pop(OriginalReplicateService, None)


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.settings.photo_generation_cost", 0.1)
@patch("orchestra.web.api.assistant.views.settings.is_staging", False)
async def test_generate_photo_success(client: AsyncClient, mock_services_factory):
    """Test successful photo generation with sufficient credits."""
    replicate_mock, users_dao_mock = mock_services_factory
    user_id = "test-user-generate-ok"

    # Configure mock for a user with enough credits
    mock_user = MagicMock()
    mock_user.credits = 10.0
    users_dao_mock.get_user_with_id.return_value = mock_user

    payload = {"prompt": "A successful test prompt"}
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

    # Verify correct calls were made
    # FIX: Use keyword argument `id` to match the method signature and avoid mock ambiguity.
    users_dao_mock.get_user_with_id.assert_called_once_with(id=user_id)
    replicate_mock.generate_photo.assert_called_once_with(
        prompt=payload["prompt"],
        aspect_ratio="1:1",
        output_format="webp",
        output_quality=80,
        safety_tolerance=2.0,
        prompt_upsampling=True,
    )
    users_dao_mock.recharge_credit.assert_called_once_with(
        user_id=user_id,
        quantity=-0.1,
    )


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.settings.photo_generation_cost", 0.1)
@patch("orchestra.web.api.assistant.views.settings.is_staging", False)
async def test_generate_photo_insufficient_credits(
    client: AsyncClient,
    mock_services_factory,
):
    """Test photo generation fails with insufficient credits."""
    replicate_mock, users_dao_mock = mock_services_factory
    user_id = "test-user-generate-fail-credits"

    # Configure mock for a user with not enough credits
    mock_user = MagicMock()
    mock_user.credits = 0.05
    users_dao_mock.get_user_with_id.return_value = mock_user

    payload = {"prompt": "This should fail"}
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/photo/generate",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 402
    assert "Insufficient credits" in resp.json()["detail"]

    # Verify external services were not called
    replicate_mock.generate_photo.assert_not_called()
    users_dao_mock.recharge_credit.assert_not_called()


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.settings.photo_generation_cost", 0.1)
@patch("orchestra.web.api.assistant.views.settings.is_staging", False)
async def test_generate_photo_replicate_fails(
    client: AsyncClient,
    mock_services_factory,
):
    """Test photo generation when the Replicate service returns an error."""
    replicate_mock, users_dao_mock = mock_services_factory
    user_id = "test-user-generate-fail-api"

    # Configure mocks
    mock_user = MagicMock()
    mock_user.credits = 10.0
    users_dao_mock.get_user_with_id.return_value = mock_user
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

    # Verify credit was not deducted on failure
    users_dao_mock.recharge_credit.assert_not_called()


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.settings.photo_generation_cost", 0.1)
@patch("orchestra.web.api.assistant.views.settings.is_staging", True)
async def test_generate_photo_staging_no_credit_check(
    client: AsyncClient,
    mock_services_factory,
):
    """Test that in staging, no credit check or deduction occurs."""
    replicate_mock, users_dao_mock = mock_services_factory
    user_id = "test-user-staging"

    payload = {"prompt": "A staging prompt"}
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/photo/generate",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 201
    assert "url" in resp.json()["info"]

    # Verify no credit-related calls were made
    users_dao_mock.get_user_with_id.assert_not_called()
    users_dao_mock.recharge_credit.assert_not_called()
    replicate_mock.generate_photo.assert_called_once()


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.settings.photo_generation_cost", 0.1)
@patch("orchestra.web.api.assistant.views.settings.is_staging", False)
async def test_edit_photo_success(client: AsyncClient, mock_services_factory):
    """Test successful photo editing with sufficient credits."""
    replicate_mock, users_dao_mock = mock_services_factory
    user_id = "test-user-edit-ok"

    # Configure mock for a user with enough credits
    mock_user = MagicMock()
    mock_user.credits = 10.0
    users_dao_mock.get_user_with_id.return_value = mock_user

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

    # Verify correct calls were made
    # FIX: Use keyword argument `id` to match the method signature and avoid mock ambiguity.
    users_dao_mock.get_user_with_id.assert_called_once_with(id=user_id)
    replicate_mock.edit_photo.assert_called_once_with(
        prompt=payload["prompt"],
        input_image=payload["input_image"],
        aspect_ratio="match_input_image",
        output_format="jpg",
        safety_tolerance=2.0,
    )
    users_dao_mock.recharge_credit.assert_called_once_with(
        user_id=user_id,
        quantity=-0.1,
    )


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.settings.photo_generation_cost", 0.1)
@patch("orchestra.web.api.assistant.views.settings.is_staging", False)
async def test_edit_photo_insufficient_credits(
    client: AsyncClient,
    mock_services_factory,
):
    """Test photo editing fails with insufficient credits."""
    replicate_mock, users_dao_mock = mock_services_factory
    user_id = "test-user-edit-fail-credits"

    # Configure mock for a user with not enough credits
    mock_user = MagicMock()
    mock_user.credits = 0.0
    users_dao_mock.get_user_with_id.return_value = mock_user

    payload = {
        "prompt": "This edit should fail",
        "input_image": "http://example.com/image.jpg",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/photo/edit",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 402
    assert "Insufficient credits to edit a photo" in resp.json()["detail"]

    # Verify external services were not called
    replicate_mock.edit_photo.assert_not_called()
    users_dao_mock.recharge_credit.assert_not_called()
