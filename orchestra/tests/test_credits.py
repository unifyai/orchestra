import math

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from starlette import status

from orchestra.db.dao.users_dao import UsersDAO
from orchestra.routines.recharging import recharge_credits
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS
from orchestra.web.api.admin.views import get_user


# TODO: amount has to be stored in the user
def test_positive_recharge(dbsession, worker_id) -> None:
    recharge_credits(worker_id, session=dbsession)
    users_dao = UsersDAO(dbsession)
    # user1
    simple = get_user("user1", dbsession)[0]
    assert math.isclose(simple.credits, 3.5)  # 1 + 2.5 = 3.5
    # user2
    recharge_limited = get_user("user2", session=dbsession)[0]
    assert math.isclose(recharge_limited.credits, 12.49)  # 9.99 + 2.5 = 12.49
    # user3
    recharge_not_needed_a = get_user("user3", session=dbsession)[0]
    assert math.isclose(recharge_not_needed_a.credits, 12.5)  # 10 + 2.5 = 12.5
    # user4
    recharge_not_needed_b = get_user("user4", session=dbsession)[0]
    assert math.isclose(recharge_not_needed_b.credits, 22.5)  # 20 + 2.5 = 22.5


# TODO: amount has to be stored in the user
def test_negative_recharge(dbsession, worker_id) -> None:
    # negative recharge
    recharge_credits(worker_id, amount=-0.5, session=dbsession)
    users_dao = UsersDAO(dbsession)
    # user1
    simple = get_user("user1", session=dbsession)[0]
    assert math.isclose(simple.credits, 0.5)  # 1 - 0.5 = 0.5
    # user2
    recharge_limited = get_user("user2", session=dbsession)[0]
    assert math.isclose(recharge_limited.credits, 9.49)  # 9.99 - 0.5 = 9.49
    # user3
    recharge_not_needed_a = get_user("user3", session=dbsession)[0]
    assert math.isclose(recharge_not_needed_a.credits, 9.5)  # 10 - 0.5 = 9.5
    # user4
    recharge_not_needed_b = get_user("user4", session=dbsession)[0]
    assert math.isclose(recharge_not_needed_b.credits, 19.5)  # 20 - 0.5 = 19.5


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
    url = fastapi_app.url_path_for("update_user_stripe_customer_id")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload = {
        "id": "stripe_autorecharge",
        "stripe_customer_id": "stripe_id_1234",
    }

    pre = dbsession.execute(query).all()[0][2]
    assert pre == None
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][2]
    assert post == "stripe_id_1234"


def add_spending_history_for_user(
    dbsession,
    user_id: str,
    total_spending: float = 150.0,
):
    """Add spending history for a user to meet billing requirements."""
    # Create some successful queries to generate spending
    num_queries = int(total_spending / 10)  # $10 per query
    remaining = total_spending - (num_queries * 10)

    for i in range(num_queries):
        query_insert = text(
            """
            INSERT INTO query (user_id, at, model_provider_str, endpoint_id, credits, query_body, response_body, status_code)
            VALUES (:user_id, NOW(), 'test_provider', 15, 10.0, '{}', '{}', 200)
        """,
        )
        dbsession.execute(query_insert, {"user_id": user_id})

    # Add remaining amount if any
    if remaining > 0:
        query_insert = text(
            """
            INSERT INTO query (user_id, at, model_provider_str, endpoint_id, credits, query_body, response_body, status_code)
            VALUES (:user_id, NOW(), 'test_provider', 15, :credits, '{}', '{}', 200)
        """,
        )
        dbsession.execute(query_insert, {"user_id": user_id, "credits": remaining})

    dbsession.commit()


@pytest.mark.anyio
async def test_enable_autorecharge(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the enable autorecharge endpoint."""
    # Add spending history to meet billing requirements
    add_spending_history_for_user(dbsession, "stripe_autorecharge")

    url = fastapi_app.url_path_for("update_user_autorecharge")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload_true = {
        "id": "stripe_autorecharge",
        "enable": "True",
    }
    payload_false = {
        "id": "stripe_autorecharge",
        "enable": "False",
    }

    pre = dbsession.execute(query).all()[0][3]
    assert pre == False
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload_true)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][3]
    assert post == True
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload_false)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][3]
    assert post == False


@pytest.mark.anyio
async def test_autorecharge_threshold(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the autorecharge threshold endpoint."""
    url = fastapi_app.url_path_for("update_user_autorecharge_threshold")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload = {
        "id": "stripe_autorecharge",
        "threshold": 10,
    }

    pre = dbsession.execute(query).all()[0][4]
    assert pre == -1
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][4]
    assert post == 10


@pytest.mark.anyio
async def test_autorecharge_qty(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the autorecharge qty endpoint."""
    # Add spending history to meet billing requirements
    add_spending_history_for_user(dbsession, "stripe_autorecharge")

    url = fastapi_app.url_path_for("update_user_autorecharge_qty")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload = {
        "id": "stripe_autorecharge",
        "qty": "30",  # Changed to meet minimum $25 requirement
    }

    pre = dbsession.execute(query).all()[0][5]
    assert pre == 0
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][5]
    assert post == 30  # Updated expected value


# TODO: amount has to be stored in the user
def test_initial_credits(dbsession, worker_id) -> None:
    """Test to check initial user credits from seeding data."""
    user1 = get_user("user1", dbsession)[0]
    user2 = get_user("user2", dbsession)[0]
    user3 = get_user("user3", dbsession)[0]
    user4 = get_user("user4", dbsession)[0]

    print(f"user1 initial credits: {user1.credits}")
    print(f"user2 initial credits: {user2.credits}")
    print(f"user3 initial credits: {user3.credits}")
    print(f"user4 initial credits: {user4.credits}")

    # Expected values for decimal credits
    assert user1.credits == 1
    assert math.isclose(user2.credits, 9.99)  # Should be 9.99 as decimal
    assert user3.credits == 10
    assert user4.credits == 20


if __name__ == "__main__":
    pass
