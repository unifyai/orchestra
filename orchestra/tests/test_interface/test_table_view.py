"""
Tests for the Table View API.

Tests for:
1. TableViewDAO functionality (CRUD, token generation, org_id updates)
2. Table View endpoint tests (create, list, get, update, delete)
3. Admin endpoint tests
4. Table View behavior during project transfers
"""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.table_view_dao import TableViewDAO
from orchestra.settings import settings
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


# ==================== TableViewDAO Unit Tests ====================


@pytest.mark.anyio
async def test_table_view_dao_create(client: AsyncClient, dbsession):
    """Test TableViewDAO create method."""
    user = await create_test_user(client, "table_view_dao_create@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableViewDAO_Test_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="TableViewDAO_Test_Project")
    project = projects[0][0]

    # Create table view
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        table_config={"row_limit": 100, "sort_by": "created_at"},
        project_config={"project_name": "TableViewDAO_Test_Project", "limit": 1000},
        title="Test Table View",
    )
    dbsession.commit()

    assert table_view.id is not None
    assert len(table_view.token) == 12
    assert table_view.project_id == project.id
    assert table_view.user_id == user["id"]
    assert table_view.organization_id is None
    assert table_view.title == "Test Table View"
    assert table_view.table_config["row_limit"] == 100


@pytest.mark.anyio
async def test_table_view_dao_get_by_token(client: AsyncClient, dbsession):
    """Test TableViewDAO get_by_token method."""
    user = await create_test_user(client, "table_view_dao_get_token@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableViewDAO_Token_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"], name="TableViewDAO_Token_Project"
    )
    project = projects[0][0]

    # Create table view
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        table_config={"row_limit": 50},
        project_config={"project_name": "TableViewDAO_Token_Project"},
    )
    dbsession.commit()

    # Retrieve by token
    retrieved = table_view_dao.get_by_token(table_view.token)
    assert retrieved is not None
    assert retrieved.id == table_view.id
    assert retrieved.token == table_view.token

    # Non-existent token
    not_found = table_view_dao.get_by_token("nonexistent1")
    assert not_found is None


@pytest.mark.anyio
async def test_table_view_dao_list_by_user_context(client: AsyncClient, dbsession):
    """Test TableViewDAO list_by_user_context method."""
    user = await create_test_user(client, "table_view_dao_list@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableViewDAO_List_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="TableViewDAO_List_Project")
    project = projects[0][0]

    # Create multiple table views
    table_view_dao = TableViewDAO(dbsession)
    for i in range(3):
        table_view_dao.create(
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
            table_config={"row_limit": 100 + i},
            project_config={"project_name": "TableViewDAO_List_Project"},
            title=f"Table View {i}",
        )
    dbsession.commit()

    # List table views (personal context)
    table_views, total_count = table_view_dao.list_by_user_context(
        user_id=user["id"],
        organization_id=None,
    )

    # At least our 3 table views should be returned
    assert len(table_views) >= 3
    assert total_count >= 3


@pytest.mark.anyio
async def test_table_view_dao_update(client: AsyncClient, dbsession):
    """Test TableViewDAO update method."""
    user = await create_test_user(client, "table_view_dao_update@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableViewDAO_Update_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"], name="TableViewDAO_Update_Project"
    )
    project = projects[0][0]

    # Create table view
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        table_config={"row_limit": 100},
        project_config={"project_name": "TableViewDAO_Update_Project"},
        title="Original Title",
    )
    dbsession.commit()

    # Update table view
    updated = table_view_dao.update(
        table_view_id=table_view.id,
        title="Updated Title",
        table_config={"row_limit": 200, "sort_by": "id"},
    )
    dbsession.commit()

    assert updated.title == "Updated Title"
    assert updated.table_config["row_limit"] == 200


@pytest.mark.anyio
async def test_table_view_dao_delete(client: AsyncClient, dbsession):
    """Test TableViewDAO delete method."""
    user = await create_test_user(client, "table_view_dao_delete@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableViewDAO_Delete_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"], name="TableViewDAO_Delete_Project"
    )
    project = projects[0][0]

    # Create table view
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        table_config={"row_limit": 100},
        project_config={"project_name": "TableViewDAO_Delete_Project"},
    )
    dbsession.commit()

    token = table_view.token
    table_view_id = table_view.id

    # Delete table view
    deleted = table_view_dao.delete(table_view_id)
    dbsession.commit()

    assert deleted is True

    # Verify it's gone
    not_found = table_view_dao.get_by_token(token)
    assert not_found is None


@pytest.mark.anyio
async def test_table_view_dao_update_organization_id(client: AsyncClient, dbsession):
    """Test TableViewDAO update_organization_id method."""
    user = await create_test_user(client, "table_view_dao_org_update@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Table View DAO Org Update Test"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    # Create personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableViewDAO_OrgUpdate_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"], name="TableViewDAO_OrgUpdate_Project"
    )
    project = projects[0][0]

    # Create table views
    table_view_dao = TableViewDAO(dbsession)
    for i in range(2):
        table_view_dao.create(
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
            table_config={"row_limit": 100 + i},
            project_config={"project_name": "TableViewDAO_OrgUpdate_Project"},
        )
    dbsession.commit()

    # Update organization_id
    count = table_view_dao.update_organization_id(
        project_id=project.id,
        organization_id=org_id,
    )
    dbsession.commit()

    assert count == 2

    # Verify table views are updated
    table_views, count = table_view_dao.list_by_project(project.id)
    assert count == 2
    for tv in table_views:
        assert tv.organization_id == org_id


@pytest.mark.anyio
async def test_table_view_dao_list_by_project_pagination(
    client: AsyncClient, dbsession
):
    """Test TableViewDAO list_by_project with pagination."""
    user = await create_test_user(client, "table_view_dao_list_proj@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableViewDAO_ListByProject",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="TableViewDAO_ListByProject")
    project = projects[0][0]

    # Create 5 table views
    table_view_dao = TableViewDAO(dbsession)
    for i in range(5):
        table_view_dao.create(
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
            table_config={"row_limit": 100 + i},
            project_config={},
        )
    dbsession.commit()

    # Test with limit
    results, total = table_view_dao.list_by_project(project.id, limit=2)
    assert len(results) == 2
    assert total == 5

    # Test with offset
    results2, total2 = table_view_dao.list_by_project(project.id, limit=2, offset=2)
    assert len(results2) == 2
    assert total2 == 5
    # Should be different items
    assert results[0].id != results2[0].id

    # Test project relationship is loaded (joinedload)
    for tv in results:
        assert tv.project is not None
        assert tv.project.name == "TableViewDAO_ListByProject"


@pytest.mark.anyio
async def test_table_view_dao_get_by_id_loads_project(client: AsyncClient, dbsession):
    """Test TableViewDAO get_by_id eager-loads project relationship."""
    user = await create_test_user(client, "table_view_dao_get_id@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableViewDAO_GetById",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="TableViewDAO_GetById")
    project = projects[0][0]

    # Create table view
    table_view_dao = TableViewDAO(dbsession)
    tv = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        table_config={"row_limit": 100},
        project_config={},
    )
    dbsession.commit()

    # Fetch by ID
    fetched = table_view_dao.get_by_id(tv.id)
    assert fetched is not None
    # Project should be loaded via joinedload
    assert fetched.project is not None
    assert fetched.project.name == "TableViewDAO_GetById"


# ==================== Table View Endpoint Tests ====================


@pytest.mark.anyio
async def test_create_table_view_with_config(client: AsyncClient, dbsession):
    """Test creating a table view with configuration."""
    user = await create_test_user(client, "table_view_create@test.com")

    # Create project via API
    project_response = await client.post(
        "/v0/project",
        json={"name": "table-view-create-project"},
        headers=user["headers"],
    )
    assert project_response.status_code == 200

    # Create table view
    response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {
                "row_limit": 100,
                "sort_by": "created_at",
                "sort_order": "desc",
            },
            "project_config": {
                "project_name": "table-view-create-project",
                "limit": 500,
            },
            "title": "My Table View",
        },
        headers=user["headers"],
    )

    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()

    assert "url" in data
    assert "token" in data
    assert len(data["token"]) == 12
    assert data["table_config"]["row_limit"] == 100
    assert data["table_config"]["sort_by"] == "created_at"
    assert data["table_view_metadata"]["title"] == "My Table View"
    assert data["table_view_metadata"]["project_name"] == "table-view-create-project"
    assert data["user_metadata"]["user_id"] == user["id"]
    assert data["user_metadata"]["organization_id"] is None


@pytest.mark.anyio
async def test_create_table_view_minimal(client: AsyncClient, dbsession):
    """Test creating a table view with minimal configuration."""
    user = await create_test_user(client, "table_view_create_min@test.com")

    # Create project via API
    await client.post(
        "/v0/project",
        json={"name": "table-view-min-project"},
        headers=user["headers"],
    )

    # Create table view with only required fields
    response = await client.post(
        "/v0/logs/table",
        json={
            "project_config": {
                "project_name": "table-view-min-project",
            },
        },
        headers=user["headers"],
    )

    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["table_config"] == {}


@pytest.mark.anyio
async def test_create_table_view_project_not_found(client: AsyncClient, dbsession):
    """Test creating a table view for non-existent project."""
    user = await create_test_user(client, "table_view_notfound@test.com")

    response = await client.post(
        "/v0/logs/table",
        json={
            "project_config": {
                "project_name": "nonexistent-project-12345",
            },
        },
        headers=user["headers"],
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in response.json()["detail"]


@pytest.mark.anyio
async def test_list_table_views(client: AsyncClient, dbsession):
    """Test listing table views."""
    user = await create_test_user(client, "table_view_list@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "table-view-list-project"},
        headers=user["headers"],
    )

    # Create multiple table views
    for i in range(3):
        await client.post(
            "/v0/logs/table",
            json={
                "table_config": {"row_limit": 100 + i},
                "project_config": {"project_name": "table-view-list-project"},
                "title": f"Table View {i}",
            },
            headers=user["headers"],
        )

    # List table views
    list_response = await client.get(
        "/v0/logs/tables",
        headers=user["headers"],
    )

    assert list_response.status_code == 200
    data = list_response.json()
    assert "table_views" in data
    assert "count" in data
    assert data["count"] >= 3


@pytest.mark.anyio
async def test_list_table_views_by_project(client: AsyncClient, dbsession):
    """Test listing table views filtered by project name."""
    user = await create_test_user(client, "table_view_list_project@test.com")

    # Create two projects
    await client.post(
        "/v0/project",
        json={"name": "table-view-list-project-a"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/project",
        json={"name": "table-view-list-project-b"},
        headers=user["headers"],
    )

    # Create table views in each project
    for project_name in ["table-view-list-project-a", "table-view-list-project-b"]:
        await client.post(
            "/v0/logs/table",
            json={
                "table_config": {"row_limit": 100},
                "project_config": {"project_name": project_name},
            },
            headers=user["headers"],
        )

    # List table views for project A only
    list_response = await client.get(
        "/v0/logs/tables?project_name=table-view-list-project-a",
        headers=user["headers"],
    )

    assert list_response.status_code == 200
    data = list_response.json()
    for tv in data["table_views"]:
        assert tv["project_name"] == "table-view-list-project-a"


@pytest.mark.anyio
async def test_get_table_view_by_token(client: AsyncClient, dbsession):
    """Test getting a table view by token."""
    user = await create_test_user(client, "table_view_get_token@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "table-view-get-token-project"},
        headers=user["headers"],
    )

    # Create table view
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "table-view-get-token-project"},
            "title": "Get By Token Test",
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Get table view by token
    get_response = await client.get(
        f"/v0/logs/tables/{token}",
        headers=user["headers"],
    )

    assert get_response.status_code == 200
    data = get_response.json()
    assert data["token"] == token
    assert data["table_view_metadata"]["title"] == "Get By Token Test"


@pytest.mark.anyio
async def test_get_table_view_not_found(client: AsyncClient, dbsession):
    """Test getting a non-existent table view."""
    user = await create_test_user(client, "table_view_get_notfound@test.com")

    get_response = await client.get(
        "/v0/logs/tables/nonexistent1",
        headers=user["headers"],
    )

    assert get_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_update_table_view(client: AsyncClient, dbsession):
    """Test updating a table view."""
    user = await create_test_user(client, "table_view_update@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "table-view-update-project"},
        headers=user["headers"],
    )

    # Create table view
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "table-view-update-project"},
            "title": "Original Title",
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Update table view
    update_response = await client.patch(
        f"/v0/logs/tables/{token}",
        json={
            "title": "Updated Title",
            "table_config": {"row_limit": 200, "sort_by": "id"},
        },
        headers=user["headers"],
    )

    assert update_response.status_code == 200
    data = update_response.json()
    assert data["table_view_metadata"]["title"] == "Updated Title"
    assert data["table_config"]["row_limit"] == 200


@pytest.mark.anyio
async def test_delete_table_view(client: AsyncClient, dbsession):
    """Test deleting a table view."""
    user = await create_test_user(client, "table_view_delete@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "table-view-delete-project"},
        headers=user["headers"],
    )

    # Create table view
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "table-view-delete-project"},
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Delete table view
    delete_response = await client.delete(
        f"/v0/logs/tables/{token}",
        headers=user["headers"],
    )

    assert delete_response.status_code == status.HTTP_200_OK
    assert delete_response.json()["deleted"] is True
    assert delete_response.json()["token"] == token

    # Verify it's gone
    get_response = await client.get(
        f"/v0/logs/tables/{token}",
        headers=user["headers"],
    )
    assert get_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_delete_table_view_not_found(client: AsyncClient, dbsession):
    """Test deleting a non-existent table view."""
    user = await create_test_user(client, "table_view_delete_notfound@test.com")

    delete_response = await client.delete(
        "/v0/logs/tables/nonexistent1",
        headers=user["headers"],
    )

    assert delete_response.status_code == status.HTTP_404_NOT_FOUND


# ==================== Admin Endpoint Tests ====================


@pytest.mark.anyio
async def test_admin_get_table_view(client: AsyncClient, dbsession):
    """Test admin endpoint to get table view by token."""
    user = await create_test_user(client, "table_view_admin_get@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "table-view-admin-get-project"},
        headers=user["headers"],
    )

    # Create table view
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "table-view-admin-get-project"},
            "title": "Admin Test Table View",
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Get table view via admin endpoint
    admin_response = await client.get(
        f"/v0/admin/logs/table?token={token}",
        headers=ADMIN_HEADERS,
    )

    assert admin_response.status_code == 200
    data = admin_response.json()
    assert data["user_id"] == user["id"]
    assert data["organization_id"] is None
    assert data["metadata"]["token"] == token
    assert data["metadata"]["title"] == "Admin Test Table View"


@pytest.mark.anyio
async def test_admin_get_table_view_not_found(client: AsyncClient, dbsession):
    """Test admin endpoint with non-existent token."""
    admin_response = await client.get(
        "/v0/admin/logs/table?token=nonexistent1",
        headers=ADMIN_HEADERS,
    )

    assert admin_response.status_code == status.HTTP_404_NOT_FOUND


# ==================== Project Transfer with Table Views Tests ====================


@pytest.mark.anyio
async def test_table_view_organization_id_updated_on_transfer_to_org(
    client: AsyncClient,
    dbsession,
):
    """Test that table view organization_id is updated when project is transferred to org."""
    user = await create_test_user(client, "table_view_transfer_to_org@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Table View Transfer Org"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    # Create personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableView_Transfer_To_Org_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"],
        name="TableView_Transfer_To_Org_Project",
    )
    project = projects[0][0]

    # Create table view for this project
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        table_config={"row_limit": 100},
        project_config={"project_name": "TableView_Transfer_To_Org_Project"},
    )
    dbsession.commit()

    table_view_token = table_view.token

    # Verify table view is personal
    assert table_view.organization_id is None

    # Transfer project to organization
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_200_OK

    # Refresh table view and verify organization_id updated
    dbsession.expire_all()
    updated_table_view = table_view_dao.get_by_token(table_view_token)

    assert updated_table_view.organization_id == org_id


@pytest.mark.anyio
async def test_table_view_organization_id_cleared_on_transfer_to_personal(
    client: AsyncClient,
    dbsession,
):
    """Test that table view organization_id is cleared when project is transferred to personal."""
    user = await create_test_user(client, "table_view_transfer_to_personal@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Table View Personal Transfer Org"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    # Create org project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableView_Transfer_To_Personal_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(
        organization_id=org_id,
        name="TableView_Transfer_To_Personal_Project",
    )
    project = projects[0][0]

    # Create table view for this org project
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=org_id,
        table_config={"row_limit": 100},
        project_config={"project_name": "TableView_Transfer_To_Personal_Project"},
    )
    dbsession.commit()

    table_view_token = table_view.token

    # Verify table view is organizational
    assert table_view.organization_id == org_id

    # Transfer project to personal
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-personal",
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_200_OK

    # Refresh table view and verify organization_id cleared
    dbsession.expire_all()
    updated_table_view = table_view_dao.get_by_token(table_view_token)

    assert updated_table_view.organization_id is None


@pytest.mark.anyio
async def test_table_views_deleted_on_project_deletion(client: AsyncClient, dbsession):
    """Test that table views are cascade deleted when project is deleted."""
    user = await create_test_user(client, "table_view_cascade_delete@test.com")

    # Create project via DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableView_Cascade_Delete_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"],
        name="TableView_Cascade_Delete_Project",
    )
    project = projects[0][0]

    # Create table view via DAO
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        table_config={"row_limit": 100},
        project_config={"project_name": "TableView_Cascade_Delete_Project"},
    )
    dbsession.commit()

    table_view_token = table_view.token

    # Verify table view exists
    assert table_view_dao.get_by_token(table_view_token) is not None

    # Delete project via DAO (triggers CASCADE delete on table views)
    dbsession.delete(project)
    dbsession.commit()

    # Verify table view is deleted (cascade)
    dbsession.expire_all()
    deleted_table_view = table_view_dao.get_by_token(table_view_token)
    assert deleted_table_view is None


# ==================== Organization-scoped Table View Tests ====================


@pytest.mark.anyio
async def test_create_table_view_in_organization_project(
    client: AsyncClient, dbsession
):
    """Test creating a table view in an organization project."""
    user = await create_test_user(client, "table_view_org_create@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Table View Org Create Test"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]
    org_name = org_response.json()["name"]

    # Get org API key from the organization_keys dict
    keys_response = await client.get(
        "/v0/api-keys",
        headers=user["headers"],
    )
    keys_data = keys_response.json()
    org_key = None

    # organization_keys is a dict keyed by org name
    if org_name in keys_data.get("organization_keys", {}):
        org_keys = keys_data["organization_keys"][org_name]
        if org_keys:
            org_key = org_keys[0]["key"]

    assert org_key is not None, f"No org key found. Response: {keys_data}"

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_key}",
    }

    # Create org project via API using org key
    project_response = await client.post(
        "/v0/project",
        json={"name": "table-view-org-project"},
        headers=org_headers,
    )
    assert project_response.status_code == 200, project_response.json()

    # Create table view using org API key
    response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "table-view-org-project"},
            "title": "Org Table View",
        },
        headers=org_headers,
    )

    assert response.status_code == status.HTTP_201_CREATED, response.json()
    data = response.json()

    assert data["user_metadata"]["user_id"] == user["id"]
    assert data["user_metadata"]["organization_id"] == org_id


# ==================== Access Control Tests ====================


@pytest.mark.anyio
async def test_list_table_views_only_returns_own_personal_views(
    client: AsyncClient, dbsession
):
    """Test that listing personal table views only returns the user's own views."""
    user1 = await create_test_user(client, "list_own_user1@test.com")
    user2 = await create_test_user(client, "list_own_user2@test.com")

    # User1 creates a project and table view
    await client.post(
        "/v0/project",
        json={"name": "user1-list-project"},
        headers=user1["headers"],
    )
    await client.post(
        "/v0/logs/table",
        json={"project_config": {"project_name": "user1-list-project"}},
        headers=user1["headers"],
    )

    # User2 creates a project and table view
    await client.post(
        "/v0/project",
        json={"name": "user2-list-project"},
        headers=user2["headers"],
    )
    await client.post(
        "/v0/logs/table",
        json={"project_config": {"project_name": "user2-list-project"}},
        headers=user2["headers"],
    )

    # User1 lists their table views - should NOT see User2's
    response = await client.get("/v0/logs/tables", headers=user1["headers"])
    assert response.status_code == 200

    for tv in response.json()["table_views"]:
        assert tv["project_name"] == "user1-list-project"  # Only user1's project

    # User2 lists their table views - should NOT see User1's
    response = await client.get("/v0/logs/tables", headers=user2["headers"])
    assert response.status_code == 200

    for tv in response.json()["table_views"]:
        assert tv["project_name"] == "user2-list-project"  # Only user2's project


@pytest.mark.anyio
async def test_cannot_access_other_users_personal_table_view(
    client: AsyncClient, dbsession
):
    """Test that a user cannot access another user's personal table view."""
    user1 = await create_test_user(client, "table_view_access_user1@test.com")
    user2 = await create_test_user(client, "table_view_access_user2@test.com")

    # User1 creates a project and table view
    await client.post(
        "/v0/project",
        json={"name": "user1-private-tv-project"},
        headers=user1["headers"],
    )

    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "user1-private-tv-project"},
        },
        headers=user1["headers"],
    )
    token = create_response.json()["token"]

    # User2 tries to access User1's table view
    get_response = await client.get(
        f"/v0/logs/tables/{token}",
        headers=user2["headers"],
    )

    # Should be forbidden or not found
    assert get_response.status_code in [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ]


@pytest.mark.anyio
async def test_cannot_update_other_users_table_view(client: AsyncClient, dbsession):
    """Test that a user cannot update another user's table view."""
    user1 = await create_test_user(client, "table_view_update_user1@test.com")
    user2 = await create_test_user(client, "table_view_update_user2@test.com")

    # User1 creates a project and table view
    await client.post(
        "/v0/project",
        json={"name": "user1-update-tv-project"},
        headers=user1["headers"],
    )

    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "user1-update-tv-project"},
            "title": "User1's Table View",
        },
        headers=user1["headers"],
    )
    token = create_response.json()["token"]

    # User2 tries to update User1's table view
    update_response = await client.patch(
        f"/v0/logs/tables/{token}",
        json={"title": "Hacked by User2"},
        headers=user2["headers"],
    )

    # Should be forbidden or not found
    assert update_response.status_code in [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ]


@pytest.mark.anyio
async def test_cannot_delete_other_users_table_view(client: AsyncClient, dbsession):
    """Test that a user cannot delete another user's table view."""
    user1 = await create_test_user(client, "table_view_delete_user1@test.com")
    user2 = await create_test_user(client, "table_view_delete_user2@test.com")

    # User1 creates a project and table view
    await client.post(
        "/v0/project",
        json={"name": "user1-delete-tv-project"},
        headers=user1["headers"],
    )

    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "user1-delete-tv-project"},
        },
        headers=user1["headers"],
    )
    token = create_response.json()["token"]

    # User2 tries to delete User1's table view
    delete_response = await client.delete(
        f"/v0/logs/tables/{token}",
        headers=user2["headers"],
    )

    # Should be forbidden or not found
    assert delete_response.status_code in [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ]

    # Verify table view still exists for User1
    get_response = await client.get(
        f"/v0/logs/tables/{token}",
        headers=user1["headers"],
    )
    assert get_response.status_code == 200


# ==================== Table View URL Generation Tests ====================


@pytest.mark.anyio
async def test_table_view_url_format(client: AsyncClient, dbsession):
    """Test that table view URLs are correctly formatted."""
    user = await create_test_user(client, "table_view_url_format@test.com")

    await client.post(
        "/v0/project",
        json={"name": "table-view-url-project"},
        headers=user["headers"],
    )

    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "table-view-url-project"},
        },
        headers=user["headers"],
    )

    data = create_response.json()
    token = data["token"]
    url = data["url"]

    # URL should follow format: {console_url}/table/view/{token}
    assert settings.console_url in url
    assert "/table/view/" in url
    assert token in url


# ==================== Token Uniqueness Tests ====================


@pytest.mark.anyio
async def test_table_view_tokens_are_unique(client: AsyncClient, dbsession):
    """Test that multiple table views get unique tokens."""
    user = await create_test_user(client, "table_view_unique_tokens@test.com")

    await client.post(
        "/v0/project",
        json={"name": "table-view-unique-project"},
        headers=user["headers"],
    )

    tokens = set()
    for i in range(10):
        response = await client.post(
            "/v0/logs/table",
            json={
                "table_config": {"row_limit": 100},
                "project_config": {"project_name": "table-view-unique-project"},
                "title": f"Table View {i}",
            },
            headers=user["headers"],
        )
        assert response.status_code == status.HTTP_201_CREATED
        tokens.add(response.json()["token"])

    # All tokens should be unique
    assert len(tokens) == 10


# ==================== Batch Delete Tests ====================


@pytest.mark.anyio
async def test_delete_table_views_by_project(client: AsyncClient, dbsession):
    """Test batch deleting all table views for a project."""
    user = await create_test_user(client, "table_view_batch_delete@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "table-view-batch-delete-project"},
        headers=user["headers"],
    )

    # Create multiple table views
    for i in range(5):
        await client.post(
            "/v0/logs/table",
            json={
                "table_config": {"row_limit": 100},
                "project_config": {"project_name": "table-view-batch-delete-project"},
                "title": f"Table View {i}",
            },
            headers=user["headers"],
        )

    # Verify table views exist
    list_response = await client.get(
        "/v0/logs/tables?project_name=table-view-batch-delete-project",
        headers=user["headers"],
    )
    assert list_response.json()["count"] == 5

    # Batch delete
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/tables",
        json={"project_name": "table-view-batch-delete-project"},
        headers=user["headers"],
    )

    assert delete_response.status_code == 200
    data = delete_response.json()
    assert data["deleted_count"] == 5
    assert data["project_name"] == "table-view-batch-delete-project"
    assert data["context"] is None

    # Verify table views are gone
    list_response = await client.get(
        "/v0/logs/tables?project_name=table-view-batch-delete-project",
        headers=user["headers"],
    )
    assert list_response.json()["count"] == 0


@pytest.mark.anyio
async def test_delete_table_views_by_project_and_context(
    client: AsyncClient, dbsession
):
    """Test batch deleting table views for a specific project/context pair."""
    user = await create_test_user(client, "table_view_batch_ctx_delete@test.com")

    # Create project and contexts
    org_member_dao = OrganizationMemberDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="table-view-batch-ctx-project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"], name="table-view-batch-ctx-project"
    )
    project = projects[0][0]

    context_dao.create(
        project_id=project.id,
        name="delete-me",
        description="To be deleted",
    )
    context_dao.create(
        project_id=project.id, name="keep-me", description="To be kept"
    )
    dbsession.commit()

    # Create table views with different contexts
    for i in range(3):
        await client.post(
            "/v0/logs/table",
            json={
                "table_config": {"row_limit": 100},
                "project_config": {
                    "project_name": "table-view-batch-ctx-project",
                    "context": "delete-me",
                },
                "title": f"Delete Me {i}",
            },
            headers=user["headers"],
        )

    for i in range(2):
        await client.post(
            "/v0/logs/table",
            json={
                "table_config": {"row_limit": 100},
                "project_config": {
                    "project_name": "table-view-batch-ctx-project",
                    "context": "keep-me",
                },
                "title": f"Keep Me {i}",
            },
            headers=user["headers"],
        )

    # Batch delete only delete-me context
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/tables",
        json={
            "project_name": "table-view-batch-ctx-project",
            "context": "delete-me",
        },
        headers=user["headers"],
    )

    assert delete_response.status_code == 200
    data = delete_response.json()
    assert data["deleted_count"] == 3
    assert data["context"] == "delete-me"

    # Verify only keep-me table views remain
    list_response = await client.get(
        "/v0/logs/tables?project_name=table-view-batch-ctx-project",
        headers=user["headers"],
    )
    assert list_response.json()["count"] == 2
    for tv in list_response.json()["table_views"]:
        assert "Keep Me" in tv["title"]


@pytest.mark.anyio
async def test_delete_table_views_by_project_not_found(client: AsyncClient, dbsession):
    """Test batch delete for non-existent project."""
    user = await create_test_user(client, "table_view_batch_delete_notfound@test.com")

    delete_response = await client.request(
        "DELETE",
        "/v0/logs/tables",
        json={"project_name": "nonexistent-project-12345"},
        headers=user["headers"],
    )

    assert delete_response.status_code == status.HTTP_404_NOT_FOUND


# ==================== Context Validation Tests ====================


@pytest.mark.anyio
async def test_create_table_view_nonexistent_context_fails(
    client: AsyncClient, dbsession
):
    """Test that creating a table view with a non-existent context fails."""
    user = await create_test_user(client, "table_view_ctx_validate@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableView_Context_Validation",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    # Try to create table view with non-existent context
    response = await client.post(
        "/v0/logs/table",
        headers=user["headers"],
        json={
            "table_config": {"row_limit": 100},
            "project_config": {
                "project_name": "TableView_Context_Validation",
                "context": "nonexistent_context",
            },
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in response.json()["detail"].lower()
    assert "nonexistent_context" in response.json()["detail"]


@pytest.mark.anyio
async def test_create_table_view_existing_context_succeeds(
    client: AsyncClient, dbsession
):
    """Test that creating a table view with an existing context succeeds."""
    user = await create_test_user(client, "table_view_ctx_valid@test.com")

    # Create project and context
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="TableView_Context_Valid",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="TableView_Context_Valid")
    project = projects[0][0]

    # Create a context
    context_dao.create(
        project_id=project.id,
        name="valid_context",
        description="A valid context",
    )
    dbsession.commit()

    # Create table view with valid context
    response = await client.post(
        "/v0/logs/table",
        headers=user["headers"],
        json={
            "table_config": {"row_limit": 100},
            "project_config": {
                "project_name": "TableView_Context_Valid",
                "context": "valid_context",
            },
        },
    )

    assert response.status_code == status.HTTP_201_CREATED


# ==================== Pagination Tests ====================


@pytest.mark.anyio
async def test_list_table_views_pagination(client: AsyncClient, dbsession):
    """Test pagination of table views list."""
    user = await create_test_user(client, "table_view_pagination@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "pagination-test-project"},
        headers=user["headers"],
    )

    # Create 15 table views
    for i in range(15):
        await client.post(
            "/v0/logs/table",
            json={
                "table_config": {"row_limit": 100},
                "project_config": {"project_name": "pagination-test-project"},
                "title": f"Table View {i}",
            },
            headers=user["headers"],
        )

    # Test default limit (50)
    response = await client.get("/v0/logs/tables", headers=user["headers"])
    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 15  # Total count
    assert len(data["table_views"]) >= 15  # All returned with default limit

    # Test with limit=5
    response = await client.get(
        "/v0/logs/tables?limit=5",
        headers=user["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["table_views"]) == 5
    assert data["count"] >= 15  # Total count should still be >= 15

    # Test with offset=5, limit=5
    response = await client.get(
        "/v0/logs/tables?limit=5&offset=5",
        headers=user["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["table_views"]) == 5

    # Test limit capped at 100
    response = await client.get(
        "/v0/logs/tables?limit=200",
        headers=user["headers"],
    )
    assert response.status_code == 200
    # Should work but limit is capped internally


@pytest.mark.anyio
async def test_list_table_views_pagination_invalid_params(client: AsyncClient, dbsession):
    """Test pagination with invalid parameters."""
    user = await create_test_user(client, "table_view_pagination_invalid@test.com")

    # Negative limit should fail validation
    response = await client.get(
        "/v0/logs/tables?limit=-1",
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # Negative offset should fail validation
    response = await client.get(
        "/v0/logs/tables?offset=-1",
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ==================== Update Validation Tests ====================


@pytest.mark.anyio
async def test_update_table_view_validates_project_name(client: AsyncClient, dbsession):
    """Test that updating project_config.project_name validates the new project."""
    user = await create_test_user(client, "table_view_update_project@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "update-validation-project"},
        headers=user["headers"],
    )

    # Create table view
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "update-validation-project"},
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Try to update to a non-existent project
    update_response = await client.patch(
        f"/v0/logs/tables/{token}",
        json={
            "project_config": {"project_name": "nonexistent-project-12345"},
        },
        headers=user["headers"],
    )

    assert update_response.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in update_response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_table_view_validates_context(client: AsyncClient, dbsession):
    """Test that updating project_config.context validates the context exists."""
    user = await create_test_user(client, "table_view_update_context@test.com")

    # Create project with a context
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="update-context-validation-project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"],
        name="update-context-validation-project",
    )
    project = projects[0][0]

    context_dao.create(
        project_id=project.id,
        name="valid-context",
        description="A valid context",
    )
    dbsession.commit()

    # Create table view
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {
                "project_name": "update-context-validation-project",
                "context": "valid-context",
            },
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Try to update to a non-existent context
    update_response = await client.patch(
        f"/v0/logs/tables/{token}",
        json={
            "project_config": {
                "project_name": "update-context-validation-project",
                "context": "nonexistent-context",
            },
        },
        headers=user["headers"],
    )

    assert update_response.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in update_response.json()["detail"].lower()
    assert "nonexistent-context" in update_response.json()["detail"]


@pytest.mark.anyio
async def test_update_table_view_to_different_project_requires_access(
    client: AsyncClient, dbsession
):
    """Test that updating to a different project requires write access to that project."""
    user1 = await create_test_user(client, "table_view_update_proj1@test.com")
    user2 = await create_test_user(client, "table_view_update_proj2@test.com")

    # User1 creates two projects
    await client.post(
        "/v0/project",
        json={"name": "user1-source-project"},
        headers=user1["headers"],
    )
    await client.post(
        "/v0/project",
        json={"name": "user1-target-project"},
        headers=user1["headers"],
    )

    # User2 creates a project
    await client.post(
        "/v0/project",
        json={"name": "user2-project"},
        headers=user2["headers"],
    )

    # User1 creates a table view in their source project
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "user1-source-project"},
        },
        headers=user1["headers"],
    )
    token = create_response.json()["token"]

    # User1 can update to their other project
    update_response = await client.patch(
        f"/v0/logs/tables/{token}",
        json={
            "project_config": {"project_name": "user1-target-project"},
        },
        headers=user1["headers"],
    )
    assert update_response.status_code == 200

    # User1 cannot update to User2's project
    update_response = await client.patch(
        f"/v0/logs/tables/{token}",
        json={
            "project_config": {"project_name": "user2-project"},
        },
        headers=user1["headers"],
    )
    assert update_response.status_code in [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ]


@pytest.mark.anyio
async def test_update_table_view_project_id_changes_with_project_name(
    client: AsyncClient, dbsession
):
    """Test that project_id FK is updated when project_name changes (Bug #2 fix)."""
    user = await create_test_user(client, "table_view_project_id_update@test.com")

    # Create two projects
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="source-project-for-fk-test",
        user_id=user["id"],
        organization_id=None,
    )
    project_dao.create(
        name="target-project-for-fk-test",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    source_projects = project_dao.filter(
        user_id=user["id"],
        name="source-project-for-fk-test",
    )
    source_project = source_projects[0][0]

    target_projects = project_dao.filter(
        user_id=user["id"],
        name="target-project-for-fk-test",
    )
    target_project = target_projects[0][0]

    # Create table view in source project
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "source-project-for-fk-test"},
        },
        headers=user["headers"],
    )
    assert create_response.status_code == status.HTTP_201_CREATED
    token = create_response.json()["token"]

    # Verify initial project_id via DAO
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.get_by_token(token)
    assert table_view.project_id == source_project.id

    # Update to target project
    update_response = await client.patch(
        f"/v0/logs/tables/{token}",
        json={
            "project_config": {"project_name": "target-project-for-fk-test"},
        },
        headers=user["headers"],
    )
    assert update_response.status_code == 200

    # Verify project_id FK was updated
    dbsession.expire_all()
    updated_table_view = table_view_dao.get_by_token(token)
    assert updated_table_view.project_id == target_project.id
    # project_name is no longer stored in JSONB - it comes from FK relationship
    assert "project_name" not in updated_table_view.project_config


# ==================== Updated At Tests ====================


@pytest.mark.anyio
async def test_table_view_updated_at_changes_on_update(client: AsyncClient, dbsession):
    """Test that updated_at timestamp changes when table view is modified."""
    user = await create_test_user(client, "table_view_updated_at@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "updated-at-test-project"},
        headers=user["headers"],
    )

    # Create table view
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "updated-at-test-project"},
            "title": "Original Title",
        },
        headers=user["headers"],
    )
    assert create_response.status_code == status.HTTP_201_CREATED

    token = create_response.json()["token"]
    original_created_at = create_response.json()["table_view_metadata"]["created_at"]
    original_updated_at = create_response.json()["table_view_metadata"]["updated_at"]

    # created_at and updated_at should be the same initially
    assert original_created_at == original_updated_at

    # Wait a moment to ensure timestamp difference
    import asyncio

    await asyncio.sleep(0.1)

    # Update the table view
    update_response = await client.patch(
        f"/v0/logs/tables/{token}",
        json={"title": "Updated Title"},
        headers=user["headers"],
    )
    assert update_response.status_code == 200

    new_created_at = update_response.json()["table_view_metadata"]["created_at"]
    new_updated_at = update_response.json()["table_view_metadata"]["updated_at"]

    # created_at should not change
    assert new_created_at == original_created_at

    # updated_at should be >= original (may be same if very fast)
    assert new_updated_at >= original_updated_at


# ==================== Schema Validation Tests ====================


@pytest.mark.anyio
async def test_visible_hidden_conflict_rejected(client: AsyncClient, dbsession):
    """Test that specifying both visible and hidden columns is rejected."""
    user = await create_test_user(client, "table_view_visible_hidden@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "visible-hidden-conflict-project"},
        headers=user["headers"],
    )

    # Try to create table view with both visible and hidden
    response = await client.post(
        "/v0/logs/table",
        headers=user["headers"],
        json={
            "table_config": {
                "columns": {
                    "visible": ["col1", "col2"],
                    "hidden": ["col3"],  # Conflict!
                },
            },
            "project_config": {
                "project_name": "visible-hidden-conflict-project",
            },
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "visible" in response.json()["detail"][0]["msg"].lower()


@pytest.mark.anyio
async def test_visible_only_accepted(client: AsyncClient, dbsession):
    """Test that specifying only visible columns is accepted."""
    user = await create_test_user(client, "table_view_visible_only@test.com")

    await client.post(
        "/v0/project",
        json={"name": "visible-only-project"},
        headers=user["headers"],
    )

    response = await client.post(
        "/v0/logs/table",
        headers=user["headers"],
        json={
            "table_config": {
                "columns": {
                    "visible": ["col1", "col2"],
                },
            },
            "project_config": {
                "project_name": "visible-only-project",
            },
        },
    )

    assert response.status_code == status.HTTP_201_CREATED


@pytest.mark.anyio
async def test_hidden_only_accepted(client: AsyncClient, dbsession):
    """Test that specifying only hidden columns is accepted."""
    user = await create_test_user(client, "table_view_hidden_only@test.com")

    await client.post(
        "/v0/project",
        json={"name": "hidden-only-project"},
        headers=user["headers"],
    )

    response = await client.post(
        "/v0/logs/table",
        headers=user["headers"],
        json={
            "table_config": {
                "columns": {
                    "hidden": ["col3"],
                },
            },
            "project_config": {
                "project_name": "hidden-only-project",
            },
        },
    )

    assert response.status_code == status.HTTP_201_CREATED


# =============================================================================
# Project Rename Tests - Verify project_name comes from FK, not stale JSONB
# =============================================================================


@pytest.mark.anyio
async def test_table_view_project_name_updates_on_project_rename(
    client: AsyncClient, dbsession
):
    """Test that table view shows updated project name after project is renamed.

    This is a critical test to verify project_name comes from the FK relationship,
    not from stale JSONB data.
    """
    user = await create_test_user(client, "table_view_rename_test@test.com")

    # Create project with original name
    await client.post(
        "/v0/project",
        json={"name": "original-project-name"},
        headers=user["headers"],
    )

    # Create table view
    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {"row_limit": 100},
            "project_config": {"project_name": "original-project-name"},
        },
        headers=user["headers"],
    )
    assert create_response.status_code == status.HTTP_201_CREATED
    token = create_response.json()["token"]

    # Verify initial project_name
    get_response = await client.get(
        f"/v0/logs/tables/{token}",
        headers=user["headers"],
    )
    assert get_response.status_code == 200
    assert get_response.json()["table_view_metadata"]["project_name"] == "original-project-name"

    # Rename the project
    rename_response = await client.patch(
        "/v0/project/original-project-name",
        json={"name": "renamed-project-name"},
        headers=user["headers"],
    )
    assert rename_response.status_code == 200

    # Get table view again - should show NEW project name
    get_response = await client.get(
        f"/v0/logs/tables/{token}",
        headers=user["headers"],
    )
    assert get_response.status_code == 200
    # This is the key assertion - project_name should reflect the rename
    assert get_response.json()["table_view_metadata"]["project_name"] == "renamed-project-name"


@pytest.mark.anyio
async def test_table_view_list_shows_current_project_name(
    client: AsyncClient, dbsession
):
    """Test that list endpoint shows current project name after rename."""
    user = await create_test_user(client, "table_view_list_rename@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "list-rename-project"},
        headers=user["headers"],
    )

    # Create table view
    await client.post(
        "/v0/logs/table",
        json={
            "table_config": {},
            "project_config": {"project_name": "list-rename-project"},
        },
        headers=user["headers"],
    )

    # Rename project
    await client.patch(
        "/v0/project/list-rename-project",
        json={"name": "list-renamed-project"},
        headers=user["headers"],
    )

    # List table views - should show new name
    list_response = await client.get(
        "/v0/logs/tables",
        headers=user["headers"],
    )
    assert list_response.status_code == 200

    # Find our table view and check project_name
    table_views = list_response.json()["table_views"]
    matching = [tv for tv in table_views if tv["project_name"] == "list-renamed-project"]
    assert len(matching) >= 1, "Table view should show renamed project name in list"


@pytest.mark.anyio
async def test_table_view_project_config_does_not_contain_project_name(
    client: AsyncClient, dbsession
):
    """Test that project_name is NOT stored in project_config JSONB.

    This ensures we're using the FK as the single source of truth.
    """
    user = await create_test_user(client, "table_view_no_jsonb_name@test.com")

    await client.post(
        "/v0/project",
        json={"name": "no-jsonb-name-project"},
        headers=user["headers"],
    )

    create_response = await client.post(
        "/v0/logs/table",
        json={
            "table_config": {},
            "project_config": {"project_name": "no-jsonb-name-project", "limit": 500},
        },
        headers=user["headers"],
    )
    assert create_response.status_code == status.HTTP_201_CREATED

    # Check that project_config does NOT contain project_name
    project_config = create_response.json()["project_config"]
    assert "project_name" not in project_config, \
        "project_name should not be stored in project_config JSONB"
    # But other fields should be there
    assert project_config.get("limit") == 500


@pytest.mark.anyio
async def test_create_table_view_token_collision_failure(client: AsyncClient, dbsession):
    """Test that token collision returns HTTP 503 with retry message."""
    from unittest.mock import patch

    from orchestra.db.dao.table_view_dao import TokenGenerationError

    user = await create_test_user(client, "table_view_token_collision@test.com")

    await client.post(
        "/v0/project",
        json={"name": "token-collision-proj"},
        headers=user["headers"],
    )

    # Mock the DAO to always raise TokenGenerationError
    with patch(
        "orchestra.db.dao.table_view_dao.TableViewDAO._generate_token",
        side_effect=TokenGenerationError("Failed to generate unique token"),
    ):
        create_response = await client.post(
            "/v0/logs/table",
            json={
                "table_config": {},
                "project_config": {"project_name": "token-collision-proj"},
            },
            headers=user["headers"],
        )

    assert create_response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "token" in create_response.json()["detail"].lower()
