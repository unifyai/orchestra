"""DashboardTokenDAO unit tests and API → DB roundtrip verification."""

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.dashboard_token_dao import DashboardTokenDAO
from orchestra.tests.utils import ADMIN_HEADERS, create_test_org, create_test_user

from .conftest import make_project, token_body

# ===========================================================================
# DAO unit tests
# ===========================================================================


@pytest.mark.anyio
async def test_dao_register(client: AsyncClient, dbsession: Session):
    """DAO.register persists a token and returns the ORM object."""
    user = await create_test_user(client, "dao_register@test.com")
    project = make_project(dbsession, user["id"], "dao-register-proj")

    dao = DashboardTokenDAO(dbsession)
    entry = dao.register(
        token="abcdefgh1234",
        entity_type="tile",
        context_name="dao-register-proj/Dashboards/Tiles",
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    assert entry.token == "abcdefgh1234"
    assert entry.entity_type == "tile"
    assert entry.context_name == "dao-register-proj/Dashboards/Tiles"
    assert entry.project_id == project.id
    assert entry.user_id == user["id"]
    assert entry.organization_id is None
    assert entry.created_at is not None


@pytest.mark.anyio
async def test_dao_get_by_token(client: AsyncClient, dbsession: Session):
    """DAO.get_by_token retrieves an existing entry and returns None for missing."""
    user = await create_test_user(client, "dao_get_token@test.com")
    project = make_project(dbsession, user["id"], "dao-get-token-proj")

    dao = DashboardTokenDAO(dbsession)
    dao.register(
        token="get_token_01",
        entity_type="dashboard",
        context_name="dao-get-token-proj/Dashboards/Layouts",
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    found = dao.get_by_token("get_token_01")
    assert found is not None
    assert found.entity_type == "dashboard"

    assert dao.get_by_token("nonexistent1") is None


@pytest.mark.anyio
async def test_dao_delete_by_token(client: AsyncClient, dbsession: Session):
    """DAO.delete_by_token removes the row and returns True; False if missing."""
    user = await create_test_user(client, "dao_delete_tok@test.com")
    project = make_project(dbsession, user["id"], "dao-delete-tok-proj")

    dao = DashboardTokenDAO(dbsession)
    dao.register(
        token="del_token_01",
        entity_type="tile",
        context_name="dao-delete-tok-proj/Dashboards/Tiles",
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    assert dao.delete_by_token("del_token_01") is True
    dbsession.commit()

    assert dao.get_by_token("del_token_01") is None
    assert dao.delete_by_token("del_token_01") is False


@pytest.mark.anyio
async def test_dao_register_with_org(client: AsyncClient, dbsession: Session):
    """DAO.register correctly stores organization_id when provided."""
    user = await create_test_user(client, "dao_org_tok@test.com")
    org = await create_test_org(client, user, "DAO Org Token Test")
    project = make_project(
        dbsession,
        user["id"],
        "dao-org-tok-proj",
        organization_id=org["id"],
    )

    dao = DashboardTokenDAO(dbsession)
    entry = dao.register(
        token="org_token_01",
        entity_type="tile",
        context_name="dao-org-tok-proj/Dashboards/Tiles",
        project_id=project.id,
        user_id=user["id"],
        organization_id=org["id"],
    )
    dbsession.commit()

    assert entry.organization_id == org["id"]
    found = dao.get_by_token("org_token_01")
    assert found.organization_id == org["id"]


@pytest.mark.anyio
async def test_dao_project_relationship(client: AsyncClient, dbsession: Session):
    """The ORM relationship lets us navigate token -> project."""
    user = await create_test_user(client, "dao_rel@test.com")
    project = make_project(dbsession, user["id"], "dao-rel-proj")

    dao = DashboardTokenDAO(dbsession)
    dao.register(
        token="rel_token_01",
        entity_type="tile",
        context_name="dao-rel-proj/Dashboards/Tiles",
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    entry = dao.get_by_token("rel_token_01")
    assert entry.project is not None
    assert entry.project.name == "dao-rel-proj"


@pytest.mark.anyio
async def test_dao_project_backref(client: AsyncClient, dbsession: Session):
    """The backref lets us navigate project -> dashboard_tokens."""
    user = await create_test_user(client, "dao_backref@test.com")
    project = make_project(dbsession, user["id"], "dao-backref-proj")

    dao = DashboardTokenDAO(dbsession)
    for i in range(3):
        dao.register(
            token=f"backref_tk_{i}",
            entity_type="tile",
            context_name="dao-backref-proj/Dashboards/Tiles",
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
        )
    dbsession.commit()

    dbsession.refresh(project)
    assert len(project.dashboard_tokens) == 3
    tokens = {t.token for t in project.dashboard_tokens}
    assert tokens == {"backref_tk_0", "backref_tk_1", "backref_tk_2"}


# ===========================================================================
# API → DB roundtrip verification
# ===========================================================================


@pytest.mark.anyio
async def test_api_registration_matches_db_state(
    client: AsyncClient,
    dbsession: Session,
):
    """After registering via the API, the DAO returns the same data
    that the admin resolution endpoint returns."""
    user = await create_test_user(client, "dbround@test.com")

    await client.post(
        "/v0/project",
        json={"name": "dbround-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "dbround_tk01",
            "tile",
            "dbround-proj/Dashboards/Tiles",
            "dbround-proj",
        ),
        headers=user["headers"],
    )

    api_resp = await client.get(
        "/v0/admin/dashboards/tokens/dbround_tk01",
        headers=ADMIN_HEADERS,
    )
    api_data = api_resp.json()

    dao = DashboardTokenDAO(dbsession)
    db_entry = dao.get_by_token("dbround_tk01")

    assert db_entry is not None
    assert db_entry.entity_type == api_data["entity_type"]
    assert db_entry.context_name == api_data["context_name"]
    assert db_entry.user_id == api_data["user_id"]
    assert db_entry.organization_id == api_data["organization_id"]
    assert db_entry.project_id == api_data["project_id"]


@pytest.mark.anyio
async def test_api_deletion_removes_from_db(
    client: AsyncClient,
    dbsession: Session,
):
    """After deleting via the API, the DAO confirms the row is gone."""
    user = await create_test_user(client, "dbdel@test.com")

    await client.post(
        "/v0/project",
        json={"name": "dbdel-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "dbdel_tk_01",
            "tile",
            "dbdel-proj/Dashboards/Tiles",
            "dbdel-proj",
        ),
        headers=user["headers"],
    )

    dao = DashboardTokenDAO(dbsession)
    assert dao.get_by_token("dbdel_tk_01") is not None

    await client.delete(
        "/v0/dashboards/tokens/dbdel_tk_01",
        headers=user["headers"],
    )

    assert dao.get_by_token("dbdel_tk_01") is None


@pytest.mark.anyio
async def test_project_id_stored_matches_actual_project(
    client: AsyncClient,
    dbsession: Session,
):
    """The project_id stored in dashboard_token matches the real project."""
    user = await create_test_user(client, "projmatch@test.com")

    await client.post(
        "/v0/project",
        json={"name": "projmatch-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "projm_tk_01",
            "tile",
            "projmatch-proj/Dashboards/Tiles",
            "projmatch-proj",
        ),
        headers=user["headers"],
    )

    dao = DashboardTokenDAO(dbsession)
    entry = dao.get_by_token("projm_tk_01")
    assert entry is not None
    assert entry.project is not None
    assert entry.project.name == "projmatch-proj"
    assert entry.project.user_id == user["id"]
