"""
Unit tests for Cloudflare Turnstile CAPTCHA verification.

Covers:
- Skipping validation when secret key is not configured
- Rejecting requests when secret key is configured but no token provided
- Handling successful Cloudflare verification
- Handling failed Cloudflare verification (invalid token)
- Handling network errors gracefully
- Forwarding the remote IP when provided
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_MODULE_PATH = "orchestra.db.dao.email_verification_dao"
_SETTINGS_PATH = f"{_MODULE_PATH}.settings"


@pytest.mark.anyio
async def test_skips_when_no_secret_key():
    """Returns True (skipped) when turnstile_secret_key is not configured."""
    from orchestra.db.dao.email_verification_dao import verify_turnstile_token

    with patch(_SETTINGS_PATH) as mock_settings:
        mock_settings.turnstile_secret_key = None
        result = await verify_turnstile_token("any-token")
    assert result is True


@pytest.mark.anyio
async def test_rejects_missing_token_when_configured():
    """Returns False when secret key is set but no token is provided."""
    from orchestra.db.dao.email_verification_dao import verify_turnstile_token

    with patch(_SETTINGS_PATH) as mock_settings:
        mock_settings.turnstile_secret_key = "test-secret"
        result = await verify_turnstile_token(None)
    assert result is False


@pytest.mark.anyio
async def test_rejects_empty_token_when_configured():
    """Returns False when secret key is set but token is empty string."""
    from orchestra.db.dao.email_verification_dao import verify_turnstile_token

    with patch(_SETTINGS_PATH) as mock_settings:
        mock_settings.turnstile_secret_key = "test-secret"
        result = await verify_turnstile_token("")
    assert result is False


@pytest.mark.anyio
async def test_returns_true_on_cloudflare_success():
    """Returns True when Cloudflare responds with success=True."""
    from orchestra.db.dao.email_verification_dao import verify_turnstile_token

    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True}
    mock_response.raise_for_status = MagicMock()

    with (
        patch(_SETTINGS_PATH) as mock_settings,
        patch(f"{_MODULE_PATH}.httpx.AsyncClient") as MockClient,
    ):
        mock_settings.turnstile_secret_key = "test-secret"
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=mock_client_instance,
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verify_turnstile_token("valid-token")

    assert result is True
    # Verify the correct payload was sent
    call_args = mock_client_instance.post.call_args
    assert call_args[1]["data"]["secret"] == "test-secret"
    assert call_args[1]["data"]["response"] == "valid-token"


@pytest.mark.anyio
async def test_returns_false_on_cloudflare_failure():
    """Returns False when Cloudflare responds with success=False."""
    from orchestra.db.dao.email_verification_dao import verify_turnstile_token

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "success": False,
        "error-codes": ["invalid-input-response"],
    }
    mock_response.raise_for_status = MagicMock()

    with (
        patch(_SETTINGS_PATH) as mock_settings,
        patch(f"{_MODULE_PATH}.httpx.AsyncClient") as MockClient,
    ):
        mock_settings.turnstile_secret_key = "test-secret"
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=mock_client_instance,
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verify_turnstile_token("bad-token")

    assert result is False


@pytest.mark.anyio
async def test_returns_false_on_network_error():
    """Returns False when the HTTP request to Cloudflare fails."""
    from orchestra.db.dao.email_verification_dao import verify_turnstile_token

    with (
        patch(_SETTINGS_PATH) as mock_settings,
        patch(f"{_MODULE_PATH}.httpx.AsyncClient") as MockClient,
    ):
        mock_settings.turnstile_secret_key = "test-secret"
        mock_client_instance = AsyncMock()
        mock_client_instance.post.side_effect = httpx.ConnectError("Connection refused")
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=mock_client_instance,
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verify_turnstile_token("valid-token")

    assert result is False


@pytest.mark.anyio
async def test_forwards_remote_ip():
    """Includes remoteip in the payload when provided."""
    from orchestra.db.dao.email_verification_dao import verify_turnstile_token

    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True}
    mock_response.raise_for_status = MagicMock()

    with (
        patch(_SETTINGS_PATH) as mock_settings,
        patch(f"{_MODULE_PATH}.httpx.AsyncClient") as MockClient,
    ):
        mock_settings.turnstile_secret_key = "test-secret"
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=mock_client_instance,
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verify_turnstile_token("valid-token", remote_ip="1.2.3.4")

    assert result is True
    call_args = mock_client_instance.post.call_args
    assert call_args[1]["data"]["remoteip"] == "1.2.3.4"


@pytest.mark.anyio
async def test_omits_remote_ip_when_none():
    """Does not include remoteip in payload when not provided."""
    from orchestra.db.dao.email_verification_dao import verify_turnstile_token

    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True}
    mock_response.raise_for_status = MagicMock()

    with (
        patch(_SETTINGS_PATH) as mock_settings,
        patch(f"{_MODULE_PATH}.httpx.AsyncClient") as MockClient,
    ):
        mock_settings.turnstile_secret_key = "test-secret"
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=mock_client_instance,
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verify_turnstile_token("valid-token", remote_ip=None)

    assert result is True
    call_args = mock_client_instance.post.call_args
    assert "remoteip" not in call_args[1]["data"]
