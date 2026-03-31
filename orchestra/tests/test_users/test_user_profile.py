"""
Tests for User profile fields.

Covers:
- Timezone validation
- Phone number validation and normalization
- Profile field updates
"""

import os

import pytest
from httpx import AsyncClient

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
}


# ============================================================================
# Timezone Tests
# ============================================================================


@pytest.mark.anyio
async def test_create_user_with_valid_timezone(client: AsyncClient):
    """Test user creation with valid IANA timezone."""
    url = "/v0/admin/user"
    params = {"email": "profile_tz_valid@example.com", "timezone": "Europe/London"}

    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["timezone"] == "Europe/London"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_timezone",
    [
        "foo",
        "UTC+1",
        "America/Fake_City",
        "PST",
        "GMT+5",
    ],  # EST might be valid in some systems
)
async def test_create_user_with_invalid_timezone(
    client: AsyncClient,
    invalid_timezone: str,
):
    """Test that invalid timezones are rejected."""
    url = "/v0/admin/user"
    params = {"email": "profile_tz_invalid@example.com", "timezone": invalid_timezone}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 422, response.json()
    assert "timezone" in response.json()["detail"][0]["loc"]
    assert "not a valid IANA timezone" in response.json()["detail"][0]["msg"]


@pytest.mark.anyio
async def test_update_user_timezone(client: AsyncClient):
    """Test updating user timezone."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": "profile_tz_update@example.com", "timezone": "UTC"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Update timezone
    url = "/v0/admin/user"
    params = {"user_id": user_id, "timezone": "Asia/Tokyo"}
    response = await client.put(url, json=params, headers=HEADERS)
    assert response.status_code == 200

    # Verify
    url = f"/v0/admin/user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.json()["timezone"] == "Asia/Tokyo"


# ============================================================================
# Phone Number Tests
# ============================================================================


@pytest.mark.anyio
async def test_create_user_with_phone_number(client: AsyncClient):
    """Test user creation with valid phone number (normalized to E.164)."""
    url = "/v0/admin/user"
    params = {
        "email": "profile_phone_valid@example.com",
        "phone_number": "+1 (650) 253-0000",
    }

    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    # Should be normalized to E.164 format
    assert response.json()["phone_number"] == "+16502530000"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_phone",
    ["not-a-phone", "12345", "abc123", "+1", "555-1234"],
)
async def test_create_user_with_invalid_phone_number(
    client: AsyncClient,
    invalid_phone: str,
):
    """Test that invalid phone numbers are rejected."""
    url = "/v0/admin/user"
    params = {
        "email": "profile_phone_invalid@example.com",
        "phone_number": invalid_phone,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 422, response.json()
    assert "phone_number" in response.json()["detail"][0]["loc"]


@pytest.mark.anyio
async def test_update_user_phone_number_requires_verification(client: AsyncClient):
    """Test that updating phone number without verification is rejected."""
    url = "/v0/admin/user"
    params = {"email": "profile_phone_update@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    url = "/v0/admin/user"
    params = {"user_id": user_id, "phone_number": "+44 20 7946 0958"}
    response = await client.put(url, json=params, headers=HEADERS)
    assert response.status_code == 422
    assert "verified" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_user_phone_number_with_verification(
    client: AsyncClient,
    dbsession,
):
    """Test that a verified phone number can be saved and is normalized."""
    from orchestra.db.models.orchestra_models import PhoneVerification

    url = "/v0/admin/user"
    params = {"email": "profile_phone_update_v@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Simulate a completed verification by inserting a verified record
    import datetime
    import hashlib

    phone = "+442079460958"
    now = datetime.datetime.now(datetime.timezone.utc)
    dbsession.add(
        PhoneVerification(
            user_id=user_id,
            phone_number=phone,
            phone_type="phone",
            code_hash=hashlib.sha256(b"123456").hexdigest(),
            expires_at=now + datetime.timedelta(minutes=10),
            verified_at=now,
        )
    )
    dbsession.commit()

    url = "/v0/admin/user"
    params = {"user_id": user_id, "phone_number": "+44 20 7946 0958"}
    response = await client.put(url, json=params, headers=HEADERS)
    assert response.status_code == 200

    url = f"/v0/admin/user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.json()["phone_number"] == "+442079460958"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_phone",
    ["invalid", "123", "+", "abc"],
)
async def test_update_user_with_invalid_phone_number(
    client: AsyncClient,
    invalid_phone: str,
):
    """Test that updating with invalid phone number is rejected."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": f"profile_phone_update_inv_{invalid_phone[:3]}@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Try invalid update
    url = "/v0/admin/user"
    params = {"user_id": user_id, "phone_number": invalid_phone}
    response = await client.put(url, json=params, headers=HEADERS)
    assert response.status_code == 422


@pytest.mark.anyio
async def test_phone_number_in_get_endpoints(client: AsyncClient):
    """Test that phone_number is included in GET responses."""
    # Create user with phone (using a valid US number format)
    url = "/v0/admin/user"
    params = {
        "email": "profile_phone_get@example.com",
        "phone_number": "+1 650 253 0000",
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    data = response.json()
    user_id = data.get("id")
    email = data.get("email")

    if not user_id or not email:
        pytest.skip("User creation didn't return expected fields")

    # Check in by-user-id response
    response = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "phone_number" in response.json()

    # Check in by-email response
    response = await client.get(
        f"/v0/admin/user/by-email?email={email}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "phone_number" in response.json()


# ============================================================================
# WhatsApp / Phone Cross-Verification Tests
# ============================================================================


@pytest.mark.anyio
async def test_whatsapp_skips_verification_when_same_as_phone(
    client: AsyncClient,
    dbsession,
):
    """Saving a WhatsApp number that matches the user's existing phone should
    succeed without separate verification."""
    from orchestra.db.models.orchestra_models import PhoneVerification

    import datetime
    import hashlib

    url = "/v0/admin/user"
    params = {"email": "profile_wa_cross@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    phone = "+442079460958"
    now = datetime.datetime.now(datetime.timezone.utc)

    # Verify and save phone number first
    dbsession.add(
        PhoneVerification(
            user_id=user_id,
            phone_number=phone,
            phone_type="phone",
            code_hash=hashlib.sha256(b"123456").hexdigest(),
            expires_at=now + datetime.timedelta(minutes=10),
            verified_at=now,
        )
    )
    dbsession.commit()

    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "phone_number": phone},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Now set WhatsApp to the same number — should NOT require verification
    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "whatsapp_number": phone},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify both are stored
    response = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    data = response.json()
    assert data["phone_number"] == phone
    assert data["whatsapp_number"] == phone


@pytest.mark.anyio
async def test_phone_skips_verification_when_same_as_whatsapp(
    client: AsyncClient,
    dbsession,
):
    """Saving a phone number that matches the user's existing WhatsApp should
    succeed without separate verification."""
    from orchestra.db.models.orchestra_models import PhoneVerification

    import datetime
    import hashlib

    url = "/v0/admin/user"
    params = {"email": "profile_ph_cross@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    phone = "+33612345678"
    now = datetime.datetime.now(datetime.timezone.utc)

    # Verify and save WhatsApp first
    dbsession.add(
        PhoneVerification(
            user_id=user_id,
            phone_number=phone,
            phone_type="whatsapp",
            code_hash=hashlib.sha256(b"654321").hexdigest(),
            expires_at=now + datetime.timedelta(minutes=10),
            verified_at=now,
        )
    )
    dbsession.commit()

    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "whatsapp_number": phone},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Now set phone to the same number — should NOT require verification
    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "phone_number": phone},
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_different_number_still_requires_verification(
    client: AsyncClient,
    dbsession,
):
    """A genuinely different number must still be verified."""
    from orchestra.db.models.orchestra_models import PhoneVerification

    import datetime
    import hashlib

    url = "/v0/admin/user"
    params = {"email": "profile_diff_num@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    phone_a = "+442079460958"
    phone_b = "+33612345678"
    now = datetime.datetime.now(datetime.timezone.utc)

    # Verify and save phone A
    dbsession.add(
        PhoneVerification(
            user_id=user_id,
            phone_number=phone_a,
            phone_type="phone",
            code_hash=hashlib.sha256(b"111111").hexdigest(),
            expires_at=now + datetime.timedelta(minutes=10),
            verified_at=now,
        )
    )
    dbsession.commit()

    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "phone_number": phone_a},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to set WhatsApp to a DIFFERENT number without verification
    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "whatsapp_number": phone_b},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "verified" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_cross_type_verification_record_accepted(
    client: AsyncClient,
    dbsession,
):
    """A verification record created for 'phone' should also be accepted
    when saving the same number as 'whatsapp' (and vice versa)."""
    from orchestra.db.models.orchestra_models import PhoneVerification

    import datetime
    import hashlib

    url = "/v0/admin/user"
    params = {"email": "profile_cross_type@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    phone = "+14155551234"
    now = datetime.datetime.now(datetime.timezone.utc)

    # Verify as "phone" type
    dbsession.add(
        PhoneVerification(
            user_id=user_id,
            phone_number=phone,
            phone_type="phone",
            code_hash=hashlib.sha256(b"999999").hexdigest(),
            expires_at=now + datetime.timedelta(minutes=10),
            verified_at=now,
        )
    )
    dbsession.commit()

    # Save as WhatsApp (using a phone-type verification record) — should succeed
    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "whatsapp_number": phone},
        headers=HEADERS,
    )
    assert response.status_code == 200


# ============================================================================
# Phone Number Normalization on Save
# ============================================================================


@pytest.mark.anyio
async def test_phone_number_normalized_on_save(
    client: AsyncClient,
    dbsession,
):
    """Phone numbers should be stored in E.164 format regardless of input."""
    from orchestra.db.models.orchestra_models import PhoneVerification

    import datetime
    import hashlib

    url = "/v0/admin/user"
    params = {"email": "profile_phone_norm@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    raw_input = "+44 20 7946 0958"
    normalized = "+442079460958"
    now = datetime.datetime.now(datetime.timezone.utc)

    dbsession.add(
        PhoneVerification(
            user_id=user_id,
            phone_number=normalized,
            phone_type="phone",
            code_hash=hashlib.sha256(b"123456").hexdigest(),
            expires_at=now + datetime.timedelta(minutes=10),
            verified_at=now,
        )
    )
    dbsession.commit()

    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "phone_number": raw_input},
        headers=HEADERS,
    )
    assert response.status_code == 200

    response = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.json()["phone_number"] == normalized


# ============================================================================
# Timezone Edge Cases
# ============================================================================


@pytest.mark.anyio
async def test_update_user_with_null_timezone_succeeds(client: AsyncClient):
    """Setting timezone to null should be accepted (clears it)."""
    url = "/v0/admin/user"
    params = {"email": "profile_tz_null@example.com", "timezone": "UTC"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "timezone": None},
        headers=HEADERS,
    )
    assert response.status_code == 200

    response = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.json()["timezone"] is None


@pytest.mark.anyio
async def test_keeping_same_phone_does_not_require_verification(
    client: AsyncClient,
    dbsession,
):
    """Updating a profile without changing the phone number should not require
    re-verification."""
    from orchestra.db.models.orchestra_models import PhoneVerification

    import datetime
    import hashlib

    url = "/v0/admin/user"
    params = {"email": "profile_keep_same@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    phone = "+16502530000"
    now = datetime.datetime.now(datetime.timezone.utc)

    # Verify and save initially
    dbsession.add(
        PhoneVerification(
            user_id=user_id,
            phone_number=phone,
            phone_type="phone",
            code_hash=hashlib.sha256(b"000000").hexdigest(),
            expires_at=now + datetime.timedelta(minutes=10),
            verified_at=now,
        )
    )
    dbsession.commit()

    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "phone_number": phone},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Now update bio without changing phone — should succeed without verification
    response = await client.put(
        "/v0/admin/user",
        json={"user_id": user_id, "phone_number": phone, "bio": "Updated bio"},
        headers=HEADERS,
    )
    assert response.status_code == 200
