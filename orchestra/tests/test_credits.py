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


# TODO: Limit + increase has to be stored in the user
def test_recharge(dbsession, worker_id) -> None:
    """Tests the recharge routine code."""
    recharge_credits(worker_id)
    users_dao = UsersDAO(dbsession)
    # user has (current + recharge ) < limit
    simple = get_user("recharge_simple", users_dao)[0]
    assert math.isclose(simple.credits, 3.5)
    # user has (current + recharge ) > limit
    recharge_limited = get_user("recharge_limited", users_dao)[0]
    assert math.isclose(recharge_limited.credits, 10)
    # user has current == limit
    recharge_not_needed_a = get_user("recharge_not_needed_a", users_dao)[0]
    assert recharge_not_needed_a.credits == 10
    # user has current > limit
    recharge_not_needed_b = get_user("recharge_not_needed_b", users_dao)[0]
    assert recharge_not_needed_b.credits == 20


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


@pytest.mark.anyio
async def test_enable_autorecharge(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the enable autorecharge endpoint."""
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
    url = fastapi_app.url_path_for("update_user_autorecharge_qty")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload = {
        "id": "stripe_autorecharge",
        "qty": "10",
    }

    pre = dbsession.execute(query).all()[0][5]
    assert pre == 0
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][5]
    assert post == 10


if __name__ == "__main__":
    pass
