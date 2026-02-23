import math

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from starlette import status

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.settings import settings
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS
from orchestra.web.api.admin.views import get_user


@pytest.fixture(autouse=True)
def _mock_stripe_settings(monkeypatch):
    """Set Stripe settings for tests."""
    monkeypatch.setattr(
        settings,
        "stripe_secret_key",
        "sk_test_dummy_for_mocking",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_webhook_secret",
        "whsec_test",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_skip_signature_verification",
        True,
        raising=False,
    )


# TODO: amount has to be stored in the user
def test_positive_recharge(dbsession, worker_id) -> None:
    user_dao = UserDAO(dbsession)
    ba_dao = BillingAccountDAO(dbsession)

    # Recharge each test user with 2.5 credits
    test_users = ["user1", "user2", "user3", "user4"]
    for user_id in test_users:
        user = user_dao.get_user_with_id(user_id)
        ba_dao.add_credits(user.billing_account_id, 2.5)

    dbsession.commit()

    # user1 - get_user returns list of Row tuples, need [0][0] to get User object
    simple = get_user("user1", dbsession)[0][0]
    assert math.isclose(float(simple.billing_account.credits), 3.5)  # 1 + 2.5 = 3.5
    # user2
    recharge_limited = get_user("user2", session=dbsession)[0][0]
    assert math.isclose(
        float(recharge_limited.billing_account.credits),
        12.49,
    )  # 9.99 + 2.5 = 12.49
    # user3
    recharge_not_needed_a = get_user("user3", session=dbsession)[0][0]
    assert math.isclose(
        float(recharge_not_needed_a.billing_account.credits),
        12.5,
    )  # 10 + 2.5 = 12.5
    # user4
    recharge_not_needed_b = get_user("user4", session=dbsession)[0][0]
    assert math.isclose(
        float(recharge_not_needed_b.billing_account.credits),
        22.5,
    )  # 20 + 2.5 = 22.5


# TODO: amount has to be stored in the user
def test_negative_recharge(dbsession, worker_id) -> None:
    user_dao = UserDAO(dbsession)
    ba_dao = BillingAccountDAO(dbsession)

    # Negative recharge (deduct credits) for each test user
    test_users = ["user1", "user2", "user3", "user4"]
    for user_id in test_users:
        user = user_dao.get_user_with_id(user_id)
        ba_dao.deduct_credits(user.billing_account_id, 0.5)

    dbsession.commit()

    # user1 - get_user returns list of Row tuples, need [0][0] to get User object
    simple = get_user("user1", session=dbsession)[0][0]
    assert math.isclose(float(simple.billing_account.credits), 0.5)  # 1 - 0.5 = 0.5
    # user2
    recharge_limited = get_user("user2", session=dbsession)[0][0]
    assert math.isclose(
        float(recharge_limited.billing_account.credits),
        9.49,
    )  # 9.99 - 0.5 = 9.49
    # user3
    recharge_not_needed_a = get_user("user3", session=dbsession)[0][0]
    assert math.isclose(
        float(recharge_not_needed_a.billing_account.credits),
        9.5,
    )  # 10 - 0.5 = 9.5
    # user4
    recharge_not_needed_b = get_user("user4", session=dbsession)[0][0]
    assert math.isclose(
        float(recharge_not_needed_b.billing_account.credits),
        19.5,
    )  # 20 - 0.5 = 19.5


@pytest.mark.anyio
async def test_deduct_credits_success(
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Test successful credit deduction."""
    # Get initial credits for the authenticated user
    credits_response = await client.get("/v0/credits", headers=HEADERS)
    assert credits_response.status_code == status.HTTP_200_OK
    initial_credits = credits_response.json()["credits"]

    # Deduct some credits
    deduct_amount = 0.5
    response = await client.post(
        "/v0/credits/deduct",
        headers=HEADERS,
        json={"amount": deduct_amount},
    )

    assert response.status_code == status.HTTP_200_OK
    response_data = response.json()

    assert response_data["previous_credits"] == initial_credits
    assert response_data["deducted"] == deduct_amount
    assert math.isclose(
        response_data["current_credits"],
        initial_credits - deduct_amount,
    )

    # Verify via GET /credits endpoint
    updated_response = await client.get("/v0/credits", headers=HEADERS)
    assert math.isclose(
        updated_response.json()["credits"],
        initial_credits - deduct_amount,
    )


@pytest.mark.anyio
async def test_deduct_credits_insufficient_funds(
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """Test deduction fails when user has insufficient credits."""
    # Get current credits for the authenticated user
    credits_response = await client.get("/v0/credits", headers=HEADERS)
    assert credits_response.status_code == status.HTTP_200_OK
    current_credits = credits_response.json()["credits"]

    # Try to deduct more than available
    response = await client.post(
        "/v0/credits/deduct",
        headers=HEADERS,
        json={"amount": current_credits + 1000000},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Insufficient credits" in response.json()["detail"]


@pytest.mark.anyio
async def test_deduct_credits_zero_amount(
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """Test deduction fails with zero amount."""
    response = await client.post(
        "/v0/credits/deduct",
        headers=HEADERS,
        json={"amount": 0},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_deduct_credits_negative_amount(
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """Test deduction fails with negative amount (cannot add credits)."""
    response = await client.post(
        "/v0/credits/deduct",
        headers=HEADERS,
        json={"amount": -5.0},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_deduct_credits_exact_balance(
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """Test deducting exactly the available balance succeeds."""
    # Get current credits for the authenticated user
    credits_response = await client.get("/v0/credits", headers=HEADERS)
    assert credits_response.status_code == status.HTTP_200_OK
    exact_balance = credits_response.json()["credits"]

    # Deduct exact balance
    response = await client.post(
        "/v0/credits/deduct",
        headers=HEADERS,
        json={"amount": exact_balance},
    )

    assert response.status_code == status.HTTP_200_OK
    response_data = response.json()
    assert response_data["current_credits"] == 0.0

    # Verify balance is actually 0
    updated_response = await client.get("/v0/credits", headers=HEADERS)
    assert updated_response.json()["credits"] == 0.0


@pytest.mark.anyio
async def test_deduct_credits_fractional_amount(
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """Test deducting fractional credit amounts."""
    # Deduct a fractional amount
    response = await client.post(
        "/v0/credits/deduct",
        headers=HEADERS,
        json={"amount": 0.123},
    )

    assert response.status_code == status.HTTP_200_OK
    response_data = response.json()
    assert response_data["deducted"] == 0.123


@pytest.mark.anyio
async def test_get_credits(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> float:
    """
    Checks the credits endpoint.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.

    :return: credits.
    """
    url = fastapi_app.url_path_for("get_credits")

    response = await client.get(url, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_dict = response.json()
    assert isinstance(response_dict, dict)
    assert "credits" in response_dict
    assert isinstance(response_dict["credits"], float)
    assert "id" in response_dict
    assert isinstance(response_dict["id"], str)
    assert len(response_dict.keys()) == 2

    return response_dict["credits"]


@pytest.mark.anyio
async def test_stripe_customer_id(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the stripe user id endpoint."""
    url = fastapi_app.url_path_for("update_stripe_customer_id")

    # Query the billing_account's stripe_customer_id via join
    query = text(
        """SELECT ba.stripe_customer_id
           FROM "user" u
           JOIN billing_account ba ON u.billing_account_id = ba.id
           WHERE u.id = 'stripe_autorecharge';""",
    )

    payload = {
        "id": "stripe_autorecharge",
        "stripe_customer_id": "stripe_id_1234",
    }

    pre = dbsession.execute(query).scalar()
    assert pre is None
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    dbsession.expire_all()  # Refresh to see committed changes
    post = dbsession.execute(query).scalar()
    assert post == "stripe_id_1234"


@pytest.mark.anyio
async def test_autorecharge_threshold(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the autorecharge threshold endpoint."""
    url = fastapi_app.url_path_for("update_autorecharge_threshold")

    # Query the billing_account's autorecharge_threshold via join
    query = text(
        """SELECT ba.autorecharge_threshold
           FROM "user" u
           JOIN billing_account ba ON u.billing_account_id = ba.id
           WHERE u.id = 'stripe_autorecharge';""",
    )

    payload = {
        "id": "stripe_autorecharge",
        "threshold": 10,
    }

    pre = dbsession.execute(query).scalar()
    # Test user stripe_autorecharge is seeded with -1 (see seeding.sql)
    assert float(pre) == -1
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    dbsession.expire_all()  # Refresh to see committed changes
    post = dbsession.execute(query).scalar()
    assert post == 10


@pytest.mark.anyio
async def test_autorecharge_qty(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Test autorecharge quantity endpoint validates $25 minimum."""
    # Test with valid amount above minimum - should succeed
    response = await client.put(
        "/v0/admin/autorecharge_qty",
        params={"id": "user1", "qty": 50.0},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200

    # Test with amount below minimum - should fail
    response = await client.put(
        "/v0/admin/autorecharge_qty",
        params={"id": "user1", "qty": 10.0},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 400


# ============================================================================
# User Checkout Session Tests
# ============================================================================

if __name__ == "__main__":
    pass
