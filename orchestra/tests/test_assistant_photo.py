from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.services.replicate_service import ReplicateAPIError
from orchestra.services.replicate_service import (
    ReplicateService as OriginalReplicateService,
)
from orchestra.tests.utils import HEADERS


# --- Fixtures ---
@pytest.fixture
def mock_replicate_service(fastapi_app):
    """Provides a mock ReplicateService instance and overrides the dependency."""
    mock_instance = MagicMock(spec=OriginalReplicateService)
    mock_instance.generate_photo.return_value = (
        "https://replicate.delivery/mock/generated_image.webp"
    )
    mock_instance.edit_photo.return_value = (
        "https://replicate.delivery/mock/edited_image.jpg"
    )

    fastapi_app.dependency_overrides[OriginalReplicateService] = lambda: mock_instance
    yield mock_instance
    fastapi_app.dependency_overrides.pop(OriginalReplicateService, None)


# --- Test Cases for /assistant/photo/generate ---


@pytest.mark.anyio
async def test_generate_photo_success(
    client: AsyncClient,
    mock_replicate_service: MagicMock,
):
    """Test successful photo generation with sufficient credits."""
    user_id_for_test = "test-user-id"
    payload = {
        "prompt": "A beautiful sunset over the mountains",
    }
    with patch("orchestra.web.api.assistant.views.settings.is_staging", False), patch(
        "orchestra.web.api.assistant.views.UsersDAO",
    ) as MockUsersDAO:
        mock_dao_instance = MockUsersDAO.return_value
        mock_user = MagicMock()
        mock_user.credits = Decimal("100.0")
        mock_dao_instance.get_user_with_id.return_value = mock_user

        # Patch request.state.user_id to control the user ID in the endpoint
        with patch(
            "orchestra.web.api.assistant.views.Request.state",
        ) as mock_request_state:
            mock_request_state.user_id = user_id_for_test
            resp = await client.post(
                "/v0/assistant/photo/generate",
                json=payload,
                headers=HEADERS,
            )

    assert resp.status_code == 201
    data = resp.json()["info"]
    assert data["url"] == "https://replicate.delivery/mock/generated_image.webp"

    mock_replicate_service.generate_photo.assert_called_once()
    call_args = mock_replicate_service.generate_photo.call_args[1]
    assert call_args["prompt"] == "A beautiful sunset over the mountains"

    mock_dao_instance.get_user_with_id.assert_called_once_with(user_id_for_test)
    mock_dao_instance.recharge_credit.assert_called_once_with(
        user_id=user_id_for_test,
        quantity=-0.05,
    )


@pytest.mark.anyio
async def test_generate_photo_insufficient_credits(
    client: AsyncClient,
    mock_replicate_service: MagicMock,
):
    """Test photo generation fails with insufficient credits."""
    user_id_for_test = "test-user-id"
    payload = {"prompt": "not enough credits prompt"}
    with patch("orchestra.web.api.assistant.views.settings.is_staging", False), patch(
        "orchestra.web.api.assistant.views.UsersDAO",
    ) as MockUsersDAO:
        mock_dao_instance = MockUsersDAO.return_value
        mock_user = MagicMock()
        mock_user.credits = Decimal("0.01")
        mock_dao_instance.get_user_with_id.return_value = mock_user

        with patch(
            "orchestra.web.api.assistant.views.Request.state",
        ) as mock_request_state:
            mock_request_state.user_id = user_id_for_test
            resp = await client.post(
                "/v0/assistant/photo/generate",
                json=payload,
                headers=HEADERS,
            )

    assert resp.status_code == 402
    assert "Insufficient credits" in resp.json()["detail"]

    mock_replicate_service.generate_photo.assert_not_called()
    mock_dao_instance.get_user_with_id.assert_called_once_with(user_id_for_test)
    mock_dao_instance.recharge_credit.assert_not_called()


@pytest.mark.anyio
async def test_generate_photo_staging_env(
    client: AsyncClient,
    mock_replicate_service: MagicMock,
):
    """Test photo generation bypasses credit check in staging environment."""
    payload = {"prompt": "staging prompt"}
    with patch("orchestra.web.api.assistant.views.settings.is_staging", True), patch(
        "orchestra.web.api.assistant.views.UsersDAO",
    ) as MockUsersDAO:
        with patch(
            "orchestra.web.api.assistant.views.Request.state",
        ) as mock_request_state:
            mock_request_state.user_id = "test-user-id"
            resp = await client.post(
                "/v0/assistant/photo/generate",
                json=payload,
                headers=HEADERS,
            )

        assert resp.status_code == 201
        mock_replicate_service.generate_photo.assert_called_once()
        MockUsersDAO.return_value.get_user_with_id.assert_not_called()
        MockUsersDAO.return_value.recharge_credit.assert_not_called()


@pytest.mark.anyio
async def test_generate_photo_replicate_api_error(
    client: AsyncClient,
    mock_replicate_service: MagicMock,
):
    """Test handling of Replicate API errors during generation."""
    mock_replicate_service.generate_photo.side_effect = ReplicateAPIError(
        status_code=503,
        detail="Replicate is down",
    )
    payload = {"prompt": "api error prompt"}
    with patch("orchestra.web.api.assistant.views.settings.is_staging", False), patch(
        "orchestra.web.api.assistant.views.UsersDAO",
    ) as MockUsersDAO:
        mock_dao_instance = MockUsersDAO.return_value
        mock_user = MagicMock()
        mock_user.credits = Decimal("100.0")
        mock_dao_instance.get_user_with_id.return_value = mock_user

        with patch(
            "orchestra.web.api.assistant.views.Request.state",
        ) as mock_request_state:
            mock_request_state.user_id = "test-user-id"
            resp = await client.post(
                "/v0/assistant/photo/generate",
                json=payload,
                headers=HEADERS,
            )

    assert resp.status_code == 503
    assert "Replicate is down" in resp.json()["detail"]
    mock_dao_instance.recharge_credit.assert_not_called()


# --- Test Cases for /assistant/photo/edit ---


@pytest.mark.anyio
async def test_edit_photo_success(
    client: AsyncClient,
    mock_replicate_service: MagicMock,
):
    """Test successful photo editing with sufficient credits."""
    user_id_for_test = "test-user-id"
    payload = {
        "prompt": "Make it a cubist painting",
        "input_image": "https://example.com/some_image.png",
    }
    with patch("orchestra.web.api.assistant.views.settings.is_staging", False), patch(
        "orchestra.web.api.assistant.views.UsersDAO",
    ) as MockUsersDAO:
        mock_dao_instance = MockUsersDAO.return_value
        mock_user = MagicMock()
        mock_user.credits = Decimal("100.0")
        mock_dao_instance.get_user_with_id.return_value = mock_user

        with patch(
            "orchestra.web.api.assistant.views.Request.state",
        ) as mock_request_state:
            mock_request_state.user_id = user_id_for_test
            resp = await client.post(
                "/v0/assistant/photo/edit",
                json=payload,
                headers=HEADERS,
            )

    assert resp.status_code == 201
    data = resp.json()["info"]
    assert data["url"] == "https://replicate.delivery/mock/edited_image.jpg"

    mock_replicate_service.edit_photo.assert_called_once()
    call_args = mock_replicate_service.edit_photo.call_args[1]
    assert call_args["prompt"] == "Make it a cubist painting"
    assert call_args["input_image"] == "https://example.com/some_image.png"

    mock_dao_instance.get_user_with_id.assert_called_once_with(user_id_for_test)
    mock_dao_instance.recharge_credit.assert_called_once_with(
        user_id=user_id_for_test,
        quantity=-0.05,
    )


@pytest.mark.anyio
async def test_edit_photo_insufficient_credits(
    client: AsyncClient,
    mock_replicate_service: MagicMock,
):
    """Test photo editing fails with insufficient credits."""
    user_id_for_test = "test-user-id"
    payload = {
        "prompt": "not enough credits",
        "input_image": "https://example.com/image.png",
    }
    with patch("orchestra.web.api.assistant.views.settings.is_staging", False), patch(
        "orchestra.web.api.assistant.views.UsersDAO",
    ) as MockUsersDAO:
        mock_dao_instance = MockUsersDAO.return_value
        mock_user = MagicMock()
        mock_user.credits = Decimal("0.04")
        mock_dao_instance.get_user_with_id.return_value = mock_user

        with patch(
            "orchestra.web.api.assistant.views.Request.state",
        ) as mock_request_state:
            mock_request_state.user_id = user_id_for_test
            resp = await client.post(
                "/v0/assistant/photo/edit",
                json=payload,
                headers=HEADERS,
            )

    assert resp.status_code == 402
    assert "Insufficient credits" in resp.json()["detail"]

    mock_replicate_service.edit_photo.assert_not_called()
    mock_dao_instance.get_user_with_id.assert_called_once_with(user_id_for_test)
    mock_dao_instance.recharge_credit.assert_not_called()


@pytest.mark.anyio
async def test_edit_photo_staging_env(
    client: AsyncClient,
    mock_replicate_service: MagicMock,
):
    """Test photo editing bypasses credit check in staging."""
    payload = {
        "prompt": "staging edit",
        "input_image": "https://example.com/image.png",
    }
    with patch("orchestra.web.api.assistant.views.settings.is_staging", True), patch(
        "orchestra.web.api.assistant.views.UsersDAO",
    ) as MockUsersDAO:
        with patch(
            "orchestra.web.api.assistant.views.Request.state",
        ) as mock_request_state:
            mock_request_state.user_id = "test-user-id"
            resp = await client.post(
                "/v0/assistant/photo/edit",
                json=payload,
                headers=HEADERS,
            )

        assert resp.status_code == 201
        mock_replicate_service.edit_photo.assert_called_once()
        MockUsersDAO.return_value.get_user_with_id.assert_not_called()
        MockUsersDAO.return_value.recharge_credit.assert_not_called()


@pytest.mark.anyio
async def test_edit_photo_replicate_api_error(
    client: AsyncClient,
    mock_replicate_service: MagicMock,
):
    """Test handling of Replicate API errors during editing."""
    mock_replicate_service.edit_photo.side_effect = ReplicateAPIError(
        status_code=500,
        detail="Internal Server Error at Replicate",
    )
    payload = {
        "prompt": "api error edit",
        "input_image": "https://example.com/image.png",
    }
    with patch("orchestra.web.api.assistant.views.settings.is_staging", False), patch(
        "orchestra.web.api.assistant.views.UsersDAO",
    ) as MockUsersDAO:
        mock_dao_instance = MockUsersDAO.return_value
        mock_user = MagicMock()
        mock_user.credits = Decimal("100.0")
        mock_dao_instance.get_user_with_id.return_value = mock_user

        with patch(
            "orchestra.web.api.assistant.views.Request.state",
        ) as mock_request_state:
            mock_request_state.user_id = "test-user-id"
            resp = await client.post(
                "/v0/assistant/photo/edit",
                json=payload,
                headers=HEADERS,
            )

    assert resp.status_code == 500
    assert "Internal Server Error at Replicate" in resp.json()["detail"]
    mock_dao_instance.recharge_credit.assert_not_called()
