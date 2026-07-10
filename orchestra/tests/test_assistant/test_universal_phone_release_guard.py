"""Guards preventing release of the shared universal Coordinator number.

Every Coordinator assistant shares one platform Twilio number (the "universal
unity" number). Releasing that number as part of a single assistant's teardown
tears it down for every Coordinator, so the deprovision paths must never call
out to Twilio for it. These tests pin that invariant at both layers:

* the low-level ``delete_phone_number`` backstop, and
* the ``deprovision_assistant_contacts`` cleanup path.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestra.settings import settings

UNIVERSAL_NUMBER = "+15550100015"
NORMAL_NUMBER = "+14155550123"


@pytest.fixture
def universal_us_number(monkeypatch):
    """Configure the US universal Coordinator number and clear the UK one."""
    monkeypatch.setattr(settings, "unity_coordinator_phone_us", UNIVERSAL_NUMBER)
    monkeypatch.setattr(settings, "unity_coordinator_phone_uk", None)
    return UNIVERSAL_NUMBER


def test_is_shared_platform_phone_number_matches_configured(universal_us_number):
    from orchestra.services.universal_unity_phone import is_shared_platform_phone_number

    # Configured universal number is caught even with no session available.
    assert is_shared_platform_phone_number(None, UNIVERSAL_NUMBER) is True
    # A non-universal number with no session cannot be a pool number.
    assert is_shared_platform_phone_number(None, NORMAL_NUMBER) is False
    assert is_shared_platform_phone_number(None, None) is False


@pytest.mark.anyio
async def test_delete_phone_number_refuses_universal(universal_us_number):
    """The low-level helper must not issue a DELETE for the shared number."""
    from orchestra.web.api.utils import assistant_infra

    client = MagicMock()
    client.request = AsyncMock()
    with patch.object(assistant_infra, "get_async_client", return_value=client):
        result = await assistant_infra.delete_phone_number(UNIVERSAL_NUMBER)

    assert result.get("skipped") is True
    assert result.get("reason") == "universal_unity_shared_number"
    client.request.assert_not_called()


@pytest.mark.anyio
async def test_delete_phone_number_releases_normal_number(monkeypatch):
    """A per-assistant number is still released via the comms endpoint."""
    monkeypatch.setattr(settings, "unity_coordinator_phone_us", None)
    monkeypatch.setattr(settings, "unity_coordinator_phone_uk", None)

    from orchestra.web.api.utils import assistant_infra

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"success": True})
    client = MagicMock()
    client.request = AsyncMock(return_value=response)

    with patch.object(
        assistant_infra,
        "_comms_url",
        return_value="http://comms",
    ), patch.object(assistant_infra, "get_async_client", return_value=client):
        result = await assistant_infra.delete_phone_number(NORMAL_NUMBER)

    assert result == {"success": True}
    client.request.assert_called_once()
    assert client.request.call_args.args[0] == "DELETE"


@pytest.mark.anyio
async def test_deprovision_skips_universal_phone(universal_us_number):
    """Cleanup must skip the external release for the shared universal number."""
    from orchestra.services import assistant_cleanup_service as svc

    del_mock = AsyncMock()
    with patch.object(svc, "delete_phone_number", del_mock):
        spec = svc.AssistantCleanupSpec(
            assistant_id=123,
            contacts=[
                svc.ContactCleanupSpec(
                    contact_type="phone",
                    contact_value=UNIVERSAL_NUMBER,
                    contact_id=None,
                    provider="twilio",
                    provisioned_by="platform",
                ),
            ],
        )
        result = await svc.deprovision_assistant_contacts(
            MagicMock(),
            [spec],
            soft_delete_successes=False,
        )

    del_mock.assert_not_called()
    assert result["success"] is True
    assert result["attempted"] == 1
