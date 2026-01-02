"""
Tests for the Plot API.

Tests for:
1. PlotDAO functionality (CRUD, token generation, org_id updates)
2. Plot endpoint tests (create, list, get, update, delete)
3. Admin endpoint tests
4. Plot behavior during project transfers
5. LLM inference validation
"""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.plot_dao import PlotDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.settings import settings
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user
from orchestra.web.api.plot.llm_inference import (
    PlotConfigValidationError,
    validate_plot_config,
)

# ==================== PlotDAO Unit Tests ====================


@pytest.mark.anyio
async def test_plot_dao_create(client: AsyncClient, dbsession):
    """Test PlotDAO create method."""
    user = await create_test_user(client, "plot_dao_create@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_Test_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="PlotDAO_Test_Project")
    project = projects[0][0]

    # Create plot
    plot_dao = PlotDAO(dbsession)
    plot = plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "scatter", "x_axis": "latency", "y_axis": "cost"},
        project_config={"project_name": "PlotDAO_Test_Project", "limit": 1000},
        title="Test Plot",
    )
    dbsession.commit()

    assert plot.id is not None
    assert len(plot.token) == 12
    assert plot.project_id == project.id
    assert plot.user_id == user["id"]
    assert plot.organization_id is None
    assert plot.title == "Test Plot"
    assert plot.plot_config["type"] == "scatter"


@pytest.mark.anyio
async def test_plot_dao_get_by_token(client: AsyncClient, dbsession):
    """Test PlotDAO get_by_token method."""
    user = await create_test_user(client, "plot_dao_get_token@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_Token_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="PlotDAO_Token_Project")
    project = projects[0][0]

    # Create plot
    plot_dao = PlotDAO(dbsession)
    plot = plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "bar", "x_axis": "model", "y_axis": "count"},
        project_config={"project_name": "PlotDAO_Token_Project"},
    )
    dbsession.commit()

    # Retrieve by token
    retrieved = plot_dao.get_by_token(plot.token)
    assert retrieved is not None
    assert retrieved.id == plot.id
    assert retrieved.token == plot.token

    # Non-existent token
    not_found = plot_dao.get_by_token("nonexistent1")
    assert not_found is None


@pytest.mark.anyio
async def test_plot_dao_list_by_user_context(client: AsyncClient, dbsession):
    """Test PlotDAO list_by_user_context method."""
    user = await create_test_user(client, "plot_dao_list@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_List_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="PlotDAO_List_Project")
    project = projects[0][0]

    # Create multiple plots
    plot_dao = PlotDAO(dbsession)
    for i in range(3):
        plot_dao.create(
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
            plot_config={"type": "scatter", "x_axis": f"x{i}", "y_axis": f"y{i}"},
            project_config={"project_name": "PlotDAO_List_Project"},
            title=f"Plot {i}",
        )
    dbsession.commit()

    # List plots (personal context)
    plots = plot_dao.list_by_user_context(
        user_id=user["id"],
        organization_id=None,
    )

    # At least our 3 plots should be returned
    assert len(plots) >= 3


@pytest.mark.anyio
async def test_plot_dao_update(client: AsyncClient, dbsession):
    """Test PlotDAO update method."""
    user = await create_test_user(client, "plot_dao_update@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_Update_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="PlotDAO_Update_Project")
    project = projects[0][0]

    # Create plot
    plot_dao = PlotDAO(dbsession)
    plot = plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "scatter", "x_axis": "x", "y_axis": "y"},
        project_config={"project_name": "PlotDAO_Update_Project"},
        title="Original Title",
    )
    dbsession.commit()

    # Update plot
    updated = plot_dao.update(
        plot_id=plot.id,
        title="Updated Title",
        plot_config={"type": "bar", "x_axis": "model", "y_axis": "count"},
    )
    dbsession.commit()

    assert updated.title == "Updated Title"
    assert updated.plot_config["type"] == "bar"


@pytest.mark.anyio
async def test_plot_dao_delete(client: AsyncClient, dbsession):
    """Test PlotDAO delete method."""
    user = await create_test_user(client, "plot_dao_delete@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_Delete_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="PlotDAO_Delete_Project")
    project = projects[0][0]

    # Create plot
    plot_dao = PlotDAO(dbsession)
    plot = plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "histogram", "x_axis": "value"},
        project_config={"project_name": "PlotDAO_Delete_Project"},
    )
    dbsession.commit()

    token = plot.token
    plot_id = plot.id

    # Delete plot
    deleted = plot_dao.delete(plot_id)
    dbsession.commit()

    assert deleted is True

    # Verify it's gone
    not_found = plot_dao.get_by_token(token)
    assert not_found is None


@pytest.mark.anyio
async def test_plot_dao_update_organization_id(client: AsyncClient, dbsession):
    """Test PlotDAO update_organization_id method."""
    user = await create_test_user(client, "plot_dao_org_update@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Plot DAO Org Update Test"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    # Create personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_OrgUpdate_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="PlotDAO_OrgUpdate_Project")
    project = projects[0][0]

    # Create plots
    plot_dao = PlotDAO(dbsession)
    for i in range(2):
        plot_dao.create(
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
            plot_config={"type": "scatter", "x_axis": f"x{i}", "y_axis": f"y{i}"},
            project_config={"project_name": "PlotDAO_OrgUpdate_Project"},
        )
    dbsession.commit()

    # Update organization_id
    count = plot_dao.update_organization_id(
        project_id=project.id,
        organization_id=org_id,
    )
    dbsession.commit()

    assert count == 2

    # Verify plots are updated
    plots = plot_dao.list_by_project(project.id)
    for plot in plots:
        assert plot.organization_id == org_id


# ==================== Plot Endpoint Tests ====================


@pytest.mark.anyio
async def test_create_plot_with_direct_config(client: AsyncClient, dbsession):
    """Test creating a plot with direct configuration."""
    user = await create_test_user(client, "plot_create_direct@test.com")

    # Create project via API
    project_response = await client.post(
        "/v0/project",
        json={"name": "plot-create-direct-project"},
        headers=user["headers"],
    )
    assert project_response.status_code == 200

    # Create plot
    plot_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {
                "type": "scatter",
                "x_axis": "latency",
                "y_axis": "cost",
            },
            "project_config": {
                "project_name": "plot-create-direct-project",
                "limit": 500,
            },
            "title": "Latency vs Cost",
        },
        headers=user["headers"],
    )

    assert plot_response.status_code == status.HTTP_201_CREATED
    data = plot_response.json()

    assert "url" in data
    assert "token" in data
    assert len(data["token"]) == 12
    assert data["plot_config"]["type"] == "scatter"
    assert data["plot_config"]["x_axis"] == "latency"
    assert data["plot_config"]["y_axis"] == "cost"
    assert data["plot_metadata"]["title"] == "Latency vs Cost"
    assert data["plot_metadata"]["project_name"] == "plot-create-direct-project"
    assert data["user_metadata"]["user_id"] == user["id"]
    assert data["user_metadata"]["organization_id"] is None


@pytest.mark.anyio
async def test_create_plot_missing_config_and_description(
    client: AsyncClient,
    dbsession,
):
    """Test that creating a plot without config or description fails."""
    user = await create_test_user(client, "plot_create_missing@test.com")

    # Create project via API
    project_response = await client.post(
        "/v0/project",
        json={"name": "plot-create-missing-project"},
        headers=user["headers"],
    )
    assert project_response.status_code == 200

    # Create plot without config or description
    plot_response = await client.post(
        "/v0/logs/plot",
        json={
            "project_config": {
                "project_name": "plot-create-missing-project",
            },
        },
        headers=user["headers"],
    )

    assert plot_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "plot_config or description" in plot_response.json()["detail"]


@pytest.mark.anyio
async def test_create_plot_project_not_found(client: AsyncClient, dbsession):
    """Test creating a plot for non-existent project."""
    user = await create_test_user(client, "plot_create_notfound@test.com")

    plot_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {
                "type": "scatter",
                "x_axis": "x",
                "y_axis": "y",
            },
            "project_config": {
                "project_name": "nonexistent-project-12345",
            },
        },
        headers=user["headers"],
    )

    assert plot_response.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in plot_response.json()["detail"]


@pytest.mark.anyio
async def test_list_plots(client: AsyncClient, dbsession):
    """Test listing plots."""
    user = await create_test_user(client, "plot_list@test.com")

    # Create project
    project_response = await client.post(
        "/v0/project",
        json={"name": "plot-list-project"},
        headers=user["headers"],
    )
    assert project_response.status_code == 200

    # Create multiple plots
    for i in range(3):
        await client.post(
            "/v0/logs/plot",
            json={
                "plot_config": {
                    "type": "scatter",
                    "x_axis": f"x{i}",
                    "y_axis": f"y{i}",
                },
                "project_config": {"project_name": "plot-list-project"},
                "title": f"Plot {i}",
            },
            headers=user["headers"],
        )

    # List plots
    list_response = await client.get(
        "/v0/logs/plots",
        headers=user["headers"],
    )

    assert list_response.status_code == 200
    data = list_response.json()
    assert "plots" in data
    assert "count" in data
    assert data["count"] >= 3


@pytest.mark.anyio
async def test_list_plots_by_project(client: AsyncClient, dbsession):
    """Test listing plots filtered by project name."""
    user = await create_test_user(client, "plot_list_project@test.com")

    # Create two projects
    await client.post(
        "/v0/project",
        json={"name": "plot-list-project-a"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/project",
        json={"name": "plot-list-project-b"},
        headers=user["headers"],
    )

    # Create plots in each project
    for project_name in ["plot-list-project-a", "plot-list-project-b"]:
        await client.post(
            "/v0/logs/plot",
            json={
                "plot_config": {"type": "bar", "x_axis": "model", "y_axis": "count"},
                "project_config": {"project_name": project_name},
            },
            headers=user["headers"],
        )

    # List plots for project A only
    list_response = await client.get(
        "/v0/logs/plots?project_name=plot-list-project-a",
        headers=user["headers"],
    )

    assert list_response.status_code == 200
    data = list_response.json()
    for plot in data["plots"]:
        assert plot["project_name"] == "plot-list-project-a"


@pytest.mark.anyio
async def test_get_plot_by_token(client: AsyncClient, dbsession):
    """Test getting a plot by token."""
    user = await create_test_user(client, "plot_get_token@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "plot-get-token-project"},
        headers=user["headers"],
    )

    # Create plot
    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "histogram", "x_axis": "duration"},
            "project_config": {"project_name": "plot-get-token-project"},
            "title": "Duration Distribution",
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Get plot by token
    get_response = await client.get(
        f"/v0/logs/plots/{token}",
        headers=user["headers"],
    )

    assert get_response.status_code == 200
    data = get_response.json()
    assert data["token"] == token
    assert data["plot_metadata"]["title"] == "Duration Distribution"


@pytest.mark.anyio
async def test_get_plot_not_found(client: AsyncClient, dbsession):
    """Test getting a non-existent plot."""
    user = await create_test_user(client, "plot_get_notfound@test.com")

    get_response = await client.get(
        "/v0/logs/plots/nonexistent1",
        headers=user["headers"],
    )

    assert get_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_update_plot(client: AsyncClient, dbsession):
    """Test updating a plot."""
    user = await create_test_user(client, "plot_update@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "plot-update-project"},
        headers=user["headers"],
    )

    # Create plot
    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {"project_name": "plot-update-project"},
            "title": "Original Title",
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Update plot
    update_response = await client.patch(
        f"/v0/logs/plots/{token}",
        json={
            "title": "Updated Title",
            "plot_config": {
                "type": "bar",
                "x_axis": "category",
                "y_axis": "value",
            },
        },
        headers=user["headers"],
    )

    assert update_response.status_code == 200
    data = update_response.json()
    assert data["plot_metadata"]["title"] == "Updated Title"
    assert data["plot_config"]["type"] == "bar"


@pytest.mark.anyio
async def test_delete_plot(client: AsyncClient, dbsession):
    """Test deleting a plot."""
    user = await create_test_user(client, "plot_delete@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "plot-delete-project"},
        headers=user["headers"],
    )

    # Create plot
    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "line", "x_axis": "time", "y_axis": "value"},
            "project_config": {"project_name": "plot-delete-project"},
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Delete plot
    delete_response = await client.delete(
        f"/v0/logs/plots/{token}",
        headers=user["headers"],
    )

    assert delete_response.status_code == status.HTTP_204_NO_CONTENT

    # Verify it's gone
    get_response = await client.get(
        f"/v0/logs/plots/{token}",
        headers=user["headers"],
    )
    assert get_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_delete_plot_not_found(client: AsyncClient, dbsession):
    """Test deleting a non-existent plot."""
    user = await create_test_user(client, "plot_delete_notfound@test.com")

    delete_response = await client.delete(
        "/v0/logs/plots/nonexistent1",
        headers=user["headers"],
    )

    assert delete_response.status_code == status.HTTP_404_NOT_FOUND


# ==================== Admin Endpoint Tests ====================


@pytest.mark.anyio
async def test_admin_get_plot(client: AsyncClient, dbsession):
    """Test admin endpoint to get plot by token."""
    user = await create_test_user(client, "plot_admin_get@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "plot-admin-get-project"},
        headers=user["headers"],
    )

    # Create plot
    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {"project_name": "plot-admin-get-project"},
            "title": "Admin Test Plot",
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Get plot via admin endpoint
    admin_response = await client.get(
        f"/v0/admin/logs/plot?token={token}",
        headers=ADMIN_HEADERS,
    )

    assert admin_response.status_code == 200
    data = admin_response.json()
    assert data["user_id"] == user["id"]
    assert data["organization_id"] is None
    assert data["metadata"]["token"] == token
    assert data["metadata"]["title"] == "Admin Test Plot"


@pytest.mark.anyio
async def test_admin_get_plot_not_found(client: AsyncClient, dbsession):
    """Test admin endpoint with non-existent token."""
    admin_response = await client.get(
        "/v0/admin/logs/plot?token=nonexistent1",
        headers=ADMIN_HEADERS,
    )

    assert admin_response.status_code == status.HTTP_404_NOT_FOUND


# ==================== Project Transfer with Plots Tests ====================


@pytest.mark.anyio
async def test_plot_organization_id_updated_on_transfer_to_org(
    client: AsyncClient,
    dbsession,
):
    """Test that plot organization_id is updated when project is transferred to org."""
    user = await create_test_user(client, "plot_transfer_to_org@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Plot Transfer Org"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    # Create personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Plot_Transfer_To_Org_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"],
        name="Plot_Transfer_To_Org_Project",
    )
    project = projects[0][0]

    # Create plot for this project
    plot_dao = PlotDAO(dbsession)
    plot = plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "scatter", "x_axis": "x", "y_axis": "y"},
        project_config={"project_name": "Plot_Transfer_To_Org_Project"},
    )
    dbsession.commit()

    plot_token = plot.token

    # Verify plot is personal
    assert plot.organization_id is None

    # Transfer project to organization
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_200_OK

    # Refresh plot and verify organization_id updated
    dbsession.expire_all()
    updated_plot = plot_dao.get_by_token(plot_token)

    assert updated_plot.organization_id == org_id


@pytest.mark.anyio
async def test_plot_organization_id_cleared_on_transfer_to_personal(
    client: AsyncClient,
    dbsession,
):
    """Test that plot organization_id is cleared when project is transferred to personal."""
    user = await create_test_user(client, "plot_transfer_to_personal@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Plot Personal Transfer Org"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    # Create org project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Plot_Transfer_To_Personal_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(
        organization_id=org_id,
        name="Plot_Transfer_To_Personal_Project",
    )
    project = projects[0][0]

    # Create plot for this org project
    plot_dao = PlotDAO(dbsession)
    plot = plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=org_id,
        plot_config={"type": "bar", "x_axis": "model", "y_axis": "count"},
        project_config={"project_name": "Plot_Transfer_To_Personal_Project"},
    )
    dbsession.commit()

    plot_token = plot.token

    # Verify plot is organizational
    assert plot.organization_id == org_id

    # Transfer project to personal
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-personal",
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_200_OK

    # Refresh plot and verify organization_id cleared
    dbsession.expire_all()
    updated_plot = plot_dao.get_by_token(plot_token)

    assert updated_plot.organization_id is None


@pytest.mark.anyio
async def test_plots_deleted_on_project_deletion(client: AsyncClient, dbsession):
    """Test that plots are cascade deleted when project is deleted.

    This test uses the DAO directly to avoid session isolation issues with the API.
    The cascade delete is enforced at the database level.
    """
    user = await create_test_user(client, "plot_cascade_delete@test.com")

    # Create project via DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Plot_Cascade_Delete_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"],
        name="Plot_Cascade_Delete_Project",
    )
    project = projects[0][0]

    # Create plot via DAO
    plot_dao = PlotDAO(dbsession)
    plot = plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "histogram", "x_axis": "value"},
        project_config={"project_name": "Plot_Cascade_Delete_Project"},
    )
    dbsession.commit()

    plot_token = plot.token

    # Verify plot exists
    assert plot_dao.get_by_token(plot_token) is not None

    # Delete project via DAO (triggers CASCADE delete on plots)
    dbsession.delete(project)
    dbsession.commit()

    # Verify plot is deleted (cascade)
    dbsession.expire_all()
    deleted_plot = plot_dao.get_by_token(plot_token)
    assert deleted_plot is None


# ==================== LLM Inference Validation Tests ====================


def test_validate_plot_config_valid_scatter():
    """Test validation of valid scatter config."""
    config = {
        "type": "scatter",
        "x_axis": "latency",
        "y_axis": "cost",
        "confidence": 0.9,
    }
    available_fields = ["latency", "cost", "model"]

    result = validate_plot_config(config, available_fields)

    assert result["type"] == "scatter"
    assert result["x_axis"] == "latency"
    assert result["y_axis"] == "cost"


def test_validate_plot_config_valid_histogram():
    """Test validation of valid histogram config."""
    config = {
        "type": "histogram",
        "x_axis": "duration",
        "bin_count": 20,
        "confidence": 0.8,
    }
    available_fields = ["duration", "status"]

    result = validate_plot_config(config, available_fields)

    assert result["type"] == "histogram"
    assert result["x_axis"] == "duration"
    assert result["bin_count"] == 20


def test_validate_plot_config_invalid_type():
    """Test validation fails for invalid plot type."""
    config = {
        "type": "invalid_type",
        "x_axis": "x",
        "y_axis": "y",
    }
    available_fields = ["x", "y"]

    with pytest.raises(PlotConfigValidationError) as exc_info:
        validate_plot_config(config, available_fields)

    assert "Invalid or missing plot type" in str(exc_info.value)


def test_validate_plot_config_missing_required_field():
    """Test validation fails for missing required field."""
    config = {
        "type": "scatter",
        "x_axis": "x",
        # y_axis is missing
    }
    # Empty available_fields means no fallback possible
    available_fields = []

    with pytest.raises(PlotConfigValidationError) as exc_info:
        validate_plot_config(config, available_fields)

    assert "x_axis" in str(exc_info.value)  # First required field that fails


def test_validate_plot_config_field_fallback():
    """Test validation applies fallback for missing field."""
    config = {
        "type": "scatter",
        "x_axis": "x",
        # y_axis is missing but there's a candidate
    }
    available_fields = ["x", "value", "count"]  # "value" matches y_axis pattern

    result = validate_plot_config(config, available_fields)

    # Should fall back to a field matching y_axis patterns
    assert result["y_axis"] in available_fields


def test_validate_plot_config_nonexistent_field_fallback():
    """Test validation falls back when specified field doesn't exist."""
    config = {
        "type": "scatter",
        "x_axis": "nonexistent_x",
        "y_axis": "nonexistent_y",
    }
    available_fields = ["timestamp", "value"]

    result = validate_plot_config(config, available_fields)

    # Should fall back to available fields
    assert result["x_axis"] in available_fields
    assert result["y_axis"] in available_fields


def test_validate_plot_config_clamp_bin_count():
    """Test that bin_count is clamped to valid range."""
    config = {
        "type": "histogram",
        "x_axis": "duration",
        "bin_count": 200,  # Over max of 100
    }
    available_fields = ["duration"]

    result = validate_plot_config(config, available_fields)

    assert result["bin_count"] == 100


def test_validate_plot_config_default_confidence():
    """Test that confidence defaults to 0.5 when invalid."""
    config = {
        "type": "scatter",
        "x_axis": "x",
        "y_axis": "y",
        "confidence": 2.0,  # Invalid (>1)
    }
    available_fields = ["x", "y"]

    result = validate_plot_config(config, available_fields)

    assert result["confidence"] == 0.5


def test_validate_plot_config_invalid_scale():
    """Test that invalid scale falls back to linear."""
    config = {
        "type": "scatter",
        "x_axis": "x",
        "y_axis": "y",
        "scale_x": "invalid",
        "scale_y": "also_invalid",
    }
    available_fields = ["x", "y"]

    result = validate_plot_config(config, available_fields)

    assert result["scale_x"] == "linear"
    assert result["scale_y"] == "linear"


def test_validate_plot_config_invalid_group_by_ignored():
    """Test that invalid group_by is set to None."""
    config = {
        "type": "scatter",
        "x_axis": "x",
        "y_axis": "y",
        "group_by": "nonexistent_group",
    }
    available_fields = ["x", "y", "model"]

    result = validate_plot_config(config, available_fields)

    assert result["group_by"] is None


def test_validate_plot_config_sort_by_and_order():
    """Test validation of sort_by and sort_order for bar charts."""
    config = {
        "type": "bar",
        "x_axis": "model",
        "y_axis": "count",
        "sort_by": "y",
        "sort_order": "desc",
        "confidence": 0.9,
    }
    available_fields = ["model", "count"]

    result = validate_plot_config(config, available_fields)

    assert result["sort_by"] == "y"
    assert result["sort_order"] == "desc"


def test_validate_plot_config_invalid_sort_by():
    """Test that invalid sort_by is set to None."""
    config = {
        "type": "bar",
        "x_axis": "model",
        "y_axis": "count",
        "sort_by": "invalid_sort",
        "confidence": 0.9,
    }
    available_fields = ["model", "count"]

    result = validate_plot_config(config, available_fields)

    assert result["sort_by"] is None


def test_validate_plot_config_invalid_sort_order():
    """Test that invalid sort_order defaults to desc."""
    config = {
        "type": "bar",
        "x_axis": "model",
        "y_axis": "count",
        "sort_order": "invalid_order",
        "confidence": 0.9,
    }
    available_fields = ["model", "count"]

    result = validate_plot_config(config, available_fields)

    assert result["sort_order"] == "desc"


def test_validate_plot_config_title_and_labels():
    """Test validation preserves title and axis labels."""
    config = {
        "type": "scatter",
        "x_axis": "latency",
        "y_axis": "cost",
        "title": "Latency vs Cost Analysis",
        "x_label": "Response Latency (ms)",
        "y_label": "API Cost ($)",
        "confidence": 0.95,
    }
    available_fields = ["latency", "cost"]

    result = validate_plot_config(config, available_fields)

    assert result["title"] == "Latency vs Cost Analysis"
    assert result["x_label"] == "Response Latency (ms)"
    assert result["y_label"] == "API Cost ($)"


def test_validate_plot_config_title_truncation():
    """Test that very long titles are truncated."""
    config = {
        "type": "scatter",
        "x_axis": "x",
        "y_axis": "y",
        "title": "A" * 150,  # Very long title
        "confidence": 0.9,
    }
    available_fields = ["x", "y"]

    result = validate_plot_config(config, available_fields)

    assert len(result["title"]) == 100  # Truncated to 100 chars


def test_validate_plot_config_show_regression_scatter():
    """Test show_regression is kept for scatter plots."""
    config = {
        "type": "scatter",
        "x_axis": "x",
        "y_axis": "y",
        "show_regression": True,
        "confidence": 0.9,
    }
    available_fields = ["x", "y"]

    result = validate_plot_config(config, available_fields)

    assert result["show_regression"] is True


def test_validate_plot_config_show_regression_removed_for_non_scatter():
    """Test show_regression is removed for non-scatter plots."""
    config = {
        "type": "bar",
        "x_axis": "model",
        "y_axis": "count",
        "show_regression": True,  # Should be ignored
        "confidence": 0.9,
    }
    available_fields = ["model", "count"]

    result = validate_plot_config(config, available_fields)

    assert "show_regression" not in result


def test_validate_plot_config_valid_bar_with_aggregate():
    """Test validation of bar chart with aggregate."""
    config = {
        "type": "bar",
        "x_axis": "model",
        "y_axis": "latency",
        "aggregate": "mean",
        "metric": "sum",
        "confidence": 0.85,
    }
    available_fields = ["model", "latency"]

    result = validate_plot_config(config, available_fields)

    assert result["aggregate"] == "mean"
    assert result["metric"] == "sum"


def test_validate_plot_config_invalid_aggregate():
    """Test that invalid aggregate defaults to mean."""
    config = {
        "type": "bar",
        "x_axis": "model",
        "y_axis": "latency",
        "aggregate": "invalid_agg",
        "confidence": 0.9,
    }
    available_fields = ["model", "latency"]

    result = validate_plot_config(config, available_fields)

    assert result["aggregate"] == "mean"


def test_validate_plot_config_invalid_metric():
    """Test that invalid metric defaults to mean."""
    config = {
        "type": "bar",
        "x_axis": "model",
        "y_axis": "latency",
        "metric": "invalid_metric",
        "confidence": 0.9,
    }
    available_fields = ["model", "latency"]

    result = validate_plot_config(config, available_fields)

    assert result["metric"] == "mean"


def test_validate_plot_config_line_chart():
    """Test validation of line chart config."""
    config = {
        "type": "line",
        "x_axis": "timestamp",
        "y_axis": "value",
        "group_by": "model",
        "scale_y": "log",
        "confidence": 0.9,
    }
    available_fields = ["timestamp", "value", "model"]

    result = validate_plot_config(config, available_fields)

    assert result["type"] == "line"
    assert result["x_axis"] == "timestamp"
    assert result["y_axis"] == "value"
    assert result["group_by"] == "model"
    assert result["scale_y"] == "log"


def test_validate_plot_config_histogram_default_bin_count():
    """Test histogram gets default bin_count of 10 when not specified."""
    config = {
        "type": "histogram",
        "x_axis": "latency",
        "confidence": 0.9,
    }
    available_fields = ["latency"]

    result = validate_plot_config(config, available_fields)

    assert result["bin_count"] == 10


def test_validate_plot_config_bin_count_clamp_low():
    """Test that bin_count below 1 is clamped to 1."""
    config = {
        "type": "histogram",
        "x_axis": "latency",
        "bin_count": -5,
        "confidence": 0.9,
    }
    available_fields = ["latency"]

    result = validate_plot_config(config, available_fields)

    assert result["bin_count"] == 1


def test_validate_plot_config_reasoning_with_warnings():
    """Test that validation warnings are added to reasoning."""
    config = {
        "type": "scatter",
        "x_axis": "x",
        "y_axis": "nonexistent",  # Will trigger fallback warning
        "scale_x": "invalid",  # Will trigger scale warning
        "confidence": 0.9,
    }
    available_fields = ["x", "value"]

    result = validate_plot_config(config, available_fields)

    assert "Validation notes" in result.get("reasoning", "")


# ==================== Organization-scoped Plot Tests ====================


@pytest.mark.anyio
async def test_create_plot_in_organization_project(client: AsyncClient, dbsession):
    """Test creating a plot in an organization project."""
    user = await create_test_user(client, "plot_org_create@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Plot Org Create Test"},
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
        json={"name": "plot-org-project"},
        headers=org_headers,
    )
    assert project_response.status_code == 200, project_response.json()

    # Create plot using org API key
    plot_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {
                "type": "scatter",
                "x_axis": "latency",
                "y_axis": "cost",
            },
            "project_config": {"project_name": "plot-org-project"},
            "title": "Org Plot",
        },
        headers=org_headers,
    )

    assert plot_response.status_code == status.HTTP_201_CREATED, plot_response.json()
    data = plot_response.json()

    assert data["user_metadata"]["user_id"] == user["id"]
    assert data["user_metadata"]["organization_id"] == org_id


# ==================== Access Control Tests ====================


@pytest.mark.anyio
async def test_cannot_access_other_users_personal_plot(client: AsyncClient, dbsession):
    """Test that a user cannot access another user's personal plot."""
    user1 = await create_test_user(client, "plot_access_user1@test.com")
    user2 = await create_test_user(client, "plot_access_user2@test.com")

    # User1 creates a project and plot
    await client.post(
        "/v0/project",
        json={"name": "user1-private-project"},
        headers=user1["headers"],
    )

    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {"project_name": "user1-private-project"},
        },
        headers=user1["headers"],
    )
    token = create_response.json()["token"]

    # User2 tries to access User1's plot
    get_response = await client.get(
        f"/v0/logs/plots/{token}",
        headers=user2["headers"],
    )

    # Should be forbidden or not found (plot exists but no access)
    assert get_response.status_code in [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ]


@pytest.mark.anyio
async def test_cannot_update_other_users_plot(client: AsyncClient, dbsession):
    """Test that a user cannot update another user's plot."""
    user1 = await create_test_user(client, "plot_update_user1@test.com")
    user2 = await create_test_user(client, "plot_update_user2@test.com")

    # User1 creates a project and plot
    await client.post(
        "/v0/project",
        json={"name": "user1-update-project"},
        headers=user1["headers"],
    )

    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {"project_name": "user1-update-project"},
            "title": "User1's Plot",
        },
        headers=user1["headers"],
    )
    token = create_response.json()["token"]

    # User2 tries to update User1's plot
    update_response = await client.patch(
        f"/v0/logs/plots/{token}",
        json={"title": "Hacked by User2"},
        headers=user2["headers"],
    )

    # Should be forbidden or not found
    assert update_response.status_code in [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ]


@pytest.mark.anyio
async def test_cannot_delete_other_users_plot(client: AsyncClient, dbsession):
    """Test that a user cannot delete another user's plot."""
    user1 = await create_test_user(client, "plot_delete_user1@test.com")
    user2 = await create_test_user(client, "plot_delete_user2@test.com")

    # User1 creates a project and plot
    await client.post(
        "/v0/project",
        json={"name": "user1-delete-project"},
        headers=user1["headers"],
    )

    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {"project_name": "user1-delete-project"},
        },
        headers=user1["headers"],
    )
    token = create_response.json()["token"]

    # User2 tries to delete User1's plot
    delete_response = await client.delete(
        f"/v0/logs/plots/{token}",
        headers=user2["headers"],
    )

    # Should be forbidden or not found
    assert delete_response.status_code in [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ]

    # Verify plot still exists for User1
    get_response = await client.get(
        f"/v0/logs/plots/{token}",
        headers=user1["headers"],
    )
    assert get_response.status_code == 200


# ==================== Plot URL Generation Tests ====================


@pytest.mark.anyio
async def test_plot_url_format(client: AsyncClient, dbsession):
    """Test that plot URLs are correctly formatted."""
    user = await create_test_user(client, "plot_url_format@test.com")

    await client.post(
        "/v0/project",
        json={"name": "plot-url-project"},
        headers=user["headers"],
    )

    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {"project_name": "plot-url-project"},
        },
        headers=user["headers"],
    )

    data = create_response.json()
    token = data["token"]
    url = data["url"]

    # URL should follow format: {console_url}/plot/view/{token}
    assert settings.console_url in url
    assert "/plot/view/" in url
    assert token in url


# ==================== Plot Config with New Fields Tests ====================


@pytest.mark.anyio
async def test_create_plot_with_extended_config(client: AsyncClient, dbsession):
    """Test creating a plot with all extended config fields."""
    user = await create_test_user(client, "plot_extended_config@test.com")

    await client.post(
        "/v0/project",
        json={"name": "plot-extended-project"},
        headers=user["headers"],
    )

    # Create plot with all available config fields
    plot_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {
                "type": "bar",
                "x_axis": "model",
                "y_axis": "latency",
                "group_by": "status",
                "aggregate": "mean",
                "scale_y": "log",
                "metric": "sum",
                "sort_by": "y",
                "sort_order": "desc",
                "title": "Model Performance by Status",
                "x_label": "Model Name",
                "y_label": "Mean Latency (ms)",
            },
            "project_config": {"project_name": "plot-extended-project"},
        },
        headers=user["headers"],
    )

    assert plot_response.status_code == status.HTTP_201_CREATED
    data = plot_response.json()

    assert data["plot_config"]["type"] == "bar"
    assert data["plot_config"]["aggregate"] == "mean"
    assert data["plot_config"]["sort_by"] == "y"
    assert data["plot_config"]["sort_order"] == "desc"
    assert data["plot_config"]["title"] == "Model Performance by Status"
    assert data["plot_config"]["x_label"] == "Model Name"
    assert data["plot_config"]["y_label"] == "Mean Latency (ms)"


@pytest.mark.anyio
async def test_create_scatter_with_regression(client: AsyncClient, dbsession):
    """Test creating a scatter plot with regression line."""
    user = await create_test_user(client, "plot_scatter_regression@test.com")

    await client.post(
        "/v0/project",
        json={"name": "plot-regression-project"},
        headers=user["headers"],
    )

    plot_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {
                "type": "scatter",
                "x_axis": "tokens",
                "y_axis": "cost",
                "show_regression": True,
                "title": "Token Count vs Cost Correlation",
            },
            "project_config": {"project_name": "plot-regression-project"},
        },
        headers=user["headers"],
    )

    assert plot_response.status_code == status.HTTP_201_CREATED
    data = plot_response.json()

    assert data["plot_config"]["show_regression"] is True


@pytest.mark.anyio
async def test_create_histogram_with_custom_bins(client: AsyncClient, dbsession):
    """Test creating a histogram with custom bin count."""
    user = await create_test_user(client, "plot_histogram_bins@test.com")

    await client.post(
        "/v0/project",
        json={"name": "plot-histogram-project"},
        headers=user["headers"],
    )

    plot_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {
                "type": "histogram",
                "x_axis": "response_time",
                "bin_count": 50,
                "scale_x": "log",
                "title": "Response Time Distribution",
                "x_label": "Response Time (ms)",
            },
            "project_config": {"project_name": "plot-histogram-project"},
        },
        headers=user["headers"],
    )

    assert plot_response.status_code == status.HTTP_201_CREATED
    data = plot_response.json()

    assert data["plot_config"]["bin_count"] == 50
    assert data["plot_config"]["scale_x"] == "log"


# ==================== Token Uniqueness Tests ====================


@pytest.mark.anyio
async def test_plot_tokens_are_unique(client: AsyncClient, dbsession):
    """Test that multiple plots get unique tokens."""
    user = await create_test_user(client, "plot_unique_tokens@test.com")

    await client.post(
        "/v0/project",
        json={"name": "plot-unique-project"},
        headers=user["headers"],
    )

    tokens = set()
    for i in range(10):
        response = await client.post(
            "/v0/logs/plot",
            json={
                "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
                "project_config": {"project_name": "plot-unique-project"},
                "title": f"Plot {i}",
            },
            headers=user["headers"],
        )
        assert response.status_code == status.HTTP_201_CREATED
        tokens.add(response.json()["token"])

    # All tokens should be unique
    assert len(tokens) == 10


# ==================== Update Partial Fields Tests ====================


@pytest.mark.anyio
async def test_update_plot_title_only(client: AsyncClient, dbsession):
    """Test updating only the title of a plot."""
    user = await create_test_user(client, "plot_update_title@test.com")

    await client.post(
        "/v0/project",
        json={"name": "plot-title-update-project"},
        headers=user["headers"],
    )

    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {
                "type": "scatter",
                "x_axis": "x",
                "y_axis": "y",
                "scale_x": "log",
            },
            "project_config": {"project_name": "plot-title-update-project"},
            "title": "Original",
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]
    original_config = create_response.json()["plot_config"]

    # Update only title
    update_response = await client.patch(
        f"/v0/logs/plots/{token}",
        json={"title": "Updated Title"},
        headers=user["headers"],
    )

    assert update_response.status_code == 200
    data = update_response.json()

    assert data["plot_metadata"]["title"] == "Updated Title"
    # Config should remain unchanged
    assert data["plot_config"]["scale_x"] == original_config["scale_x"]


@pytest.mark.anyio
async def test_update_plot_config_only(client: AsyncClient, dbsession):
    """Test updating only the plot config."""
    user = await create_test_user(client, "plot_update_config@test.com")

    await client.post(
        "/v0/project",
        json={"name": "plot-config-update-project"},
        headers=user["headers"],
    )

    create_response = await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {"project_name": "plot-config-update-project"},
            "title": "Keep This Title",
        },
        headers=user["headers"],
    )
    token = create_response.json()["token"]

    # Update only config
    update_response = await client.patch(
        f"/v0/logs/plots/{token}",
        json={
            "plot_config": {
                "type": "bar",
                "x_axis": "category",
                "y_axis": "value",
            },
        },
        headers=user["headers"],
    )

    assert update_response.status_code == 200
    data = update_response.json()

    assert data["plot_metadata"]["title"] == "Keep This Title"
    assert data["plot_config"]["type"] == "bar"


# ==================== Empty List Tests ====================


@pytest.mark.anyio
async def test_list_plots_empty(client: AsyncClient, dbsession):
    """Test listing plots when user has none."""
    user = await create_test_user(client, "plot_list_empty@test.com")

    # No projects or plots created

    list_response = await client.get(
        "/v0/logs/plots",
        headers=user["headers"],
    )

    assert list_response.status_code == 200
    data = list_response.json()
    assert data["count"] == 0
    assert data["plots"] == []


@pytest.mark.anyio
async def test_list_plots_nonexistent_project(client: AsyncClient, dbsession):
    """Test listing plots for a project that doesn't exist."""
    user = await create_test_user(client, "plot_list_noproject@test.com")

    list_response = await client.get(
        "/v0/logs/plots?project_name=nonexistent-project",
        headers=user["headers"],
    )

    assert list_response.status_code == 200
    data = list_response.json()
    assert data["count"] == 0


# ==================== Context Filter Tests ====================


@pytest.mark.anyio
async def test_list_plots_by_context(client: AsyncClient, dbsession):
    """Test listing plots filtered by context."""
    user = await create_test_user(client, "plot_list_context@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "plot-context-project"},
        headers=user["headers"],
    )

    # Create plots with different contexts
    await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {
                "project_name": "plot-context-project",
                "context": "context-a",
            },
            "title": "Plot Context A",
        },
        headers=user["headers"],
    )

    await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "bar", "x_axis": "model", "y_axis": "count"},
            "project_config": {
                "project_name": "plot-context-project",
                "context": "context-b",
            },
            "title": "Plot Context B",
        },
        headers=user["headers"],
    )

    await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "histogram", "x_axis": "latency"},
            "project_config": {
                "project_name": "plot-context-project",
                "context": "context-a",
            },
            "title": "Plot Context A 2",
        },
        headers=user["headers"],
    )

    # List plots with context-a filter
    list_response = await client.get(
        "/v0/logs/plots?project_name=plot-context-project&context=context-a",
        headers=user["headers"],
    )

    assert list_response.status_code == 200
    data = list_response.json()
    assert data["count"] == 2
    for plot in data["plots"]:
        assert "Context A" in plot["title"]


@pytest.mark.anyio
async def test_list_plots_context_without_project(client: AsyncClient, dbsession):
    """Test listing plots by context without project filter."""
    user = await create_test_user(client, "plot_list_context_only@test.com")

    # Create two projects
    await client.post(
        "/v0/project",
        json={"name": "plot-ctx-project-1"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/project",
        json={"name": "plot-ctx-project-2"},
        headers=user["headers"],
    )

    # Create plots in different projects with same context
    await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {
                "project_name": "plot-ctx-project-1",
                "context": "shared-context",
            },
            "title": "Project 1 Shared",
        },
        headers=user["headers"],
    )

    await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {
                "project_name": "plot-ctx-project-2",
                "context": "shared-context",
            },
            "title": "Project 2 Shared",
        },
        headers=user["headers"],
    )

    await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {
                "project_name": "plot-ctx-project-1",
                "context": "other-context",
            },
            "title": "Project 1 Other",
        },
        headers=user["headers"],
    )

    # List all plots with shared-context (across all projects)
    list_response = await client.get(
        "/v0/logs/plots?context=shared-context",
        headers=user["headers"],
    )

    assert list_response.status_code == 200
    data = list_response.json()
    assert data["count"] == 2
    titles = [plot["title"] for plot in data["plots"]]
    assert "Project 1 Shared" in titles
    assert "Project 2 Shared" in titles


@pytest.mark.anyio
async def test_plot_dao_list_by_user_context_with_context_filter(
    client: AsyncClient,
    dbsession,
):
    """Test PlotDAO list_by_user_context with context filter."""
    user = await create_test_user(client, "plot_dao_ctx_filter@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_Context_Filter_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"],
        name="PlotDAO_Context_Filter_Project",
    )
    project = projects[0][0]

    # Create plots with different contexts
    plot_dao = PlotDAO(dbsession)
    plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "scatter", "x_axis": "x", "y_axis": "y"},
        project_config={
            "project_name": "PlotDAO_Context_Filter_Project",
            "context": "ctx1",
        },
        title="Plot ctx1",
    )
    plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "bar", "x_axis": "model", "y_axis": "count"},
        project_config={
            "project_name": "PlotDAO_Context_Filter_Project",
            "context": "ctx2",
        },
        title="Plot ctx2",
    )
    plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "histogram", "x_axis": "latency"},
        project_config={
            "project_name": "PlotDAO_Context_Filter_Project",
            "context": "ctx1",
        },
        title="Plot ctx1 second",
    )
    dbsession.commit()

    # Filter by ctx1
    plots = plot_dao.list_by_user_context(
        user_id=user["id"],
        organization_id=None,
        project_id=project.id,
        context="ctx1",
    )

    assert len(plots) == 2
    for plot in plots:
        assert plot.project_config.get("context") == "ctx1"


# ==================== Batch Delete Tests ====================


@pytest.mark.anyio
async def test_delete_plots_by_project(client: AsyncClient, dbsession):
    """Test batch deleting all plots for a project."""
    user = await create_test_user(client, "plot_batch_delete@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "plot-batch-delete-project"},
        headers=user["headers"],
    )

    # Create multiple plots
    for i in range(5):
        await client.post(
            "/v0/logs/plot",
            json={
                "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
                "project_config": {"project_name": "plot-batch-delete-project"},
                "title": f"Plot {i}",
            },
            headers=user["headers"],
        )

    # Verify plots exist
    list_response = await client.get(
        "/v0/logs/plots?project_name=plot-batch-delete-project",
        headers=user["headers"],
    )
    assert list_response.json()["count"] == 5

    # Batch delete
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/plots",
        json={"project_name": "plot-batch-delete-project"},
        headers=user["headers"],
    )

    assert delete_response.status_code == 200
    data = delete_response.json()
    assert data["deleted_count"] == 5
    assert data["project_name"] == "plot-batch-delete-project"
    assert data["context"] is None

    # Verify plots are gone
    list_response = await client.get(
        "/v0/logs/plots?project_name=plot-batch-delete-project",
        headers=user["headers"],
    )
    assert list_response.json()["count"] == 0


@pytest.mark.anyio
async def test_delete_plots_by_project_and_context(client: AsyncClient, dbsession):
    """Test batch deleting plots for a specific project/context pair."""
    user = await create_test_user(client, "plot_batch_ctx_delete@test.com")

    # Create project
    await client.post(
        "/v0/project",
        json={"name": "plot-batch-ctx-project"},
        headers=user["headers"],
    )

    # Create plots with different contexts
    for i in range(3):
        await client.post(
            "/v0/logs/plot",
            json={
                "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
                "project_config": {
                    "project_name": "plot-batch-ctx-project",
                    "context": "delete-me",
                },
                "title": f"Delete Me {i}",
            },
            headers=user["headers"],
        )

    for i in range(2):
        await client.post(
            "/v0/logs/plot",
            json={
                "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
                "project_config": {
                    "project_name": "plot-batch-ctx-project",
                    "context": "keep-me",
                },
                "title": f"Keep Me {i}",
            },
            headers=user["headers"],
        )

    # Batch delete only delete-me context
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/plots",
        json={
            "project_name": "plot-batch-ctx-project",
            "context": "delete-me",
        },
        headers=user["headers"],
    )

    assert delete_response.status_code == 200
    data = delete_response.json()
    assert data["deleted_count"] == 3
    assert data["context"] == "delete-me"

    # Verify only keep-me plots remain
    list_response = await client.get(
        "/v0/logs/plots?project_name=plot-batch-ctx-project",
        headers=user["headers"],
    )
    assert list_response.json()["count"] == 2
    for plot in list_response.json()["plots"]:
        assert "Keep Me" in plot["title"]


@pytest.mark.anyio
async def test_delete_plots_by_project_not_found(client: AsyncClient, dbsession):
    """Test batch delete for non-existent project."""
    user = await create_test_user(client, "plot_batch_delete_notfound@test.com")

    delete_response = await client.request(
        "DELETE",
        "/v0/logs/plots",
        json={"project_name": "nonexistent-project-12345"},
        headers=user["headers"],
    )

    assert delete_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_delete_plots_by_project_no_access(client: AsyncClient, dbsession):
    """Test batch delete for project user doesn't have write access to."""
    user1 = await create_test_user(client, "plot_batch_user1@test.com")
    user2 = await create_test_user(client, "plot_batch_user2@test.com")

    # User1 creates project and plot
    await client.post(
        "/v0/project",
        json={"name": "user1-batch-project"},
        headers=user1["headers"],
    )

    await client.post(
        "/v0/logs/plot",
        json={
            "plot_config": {"type": "scatter", "x_axis": "x", "y_axis": "y"},
            "project_config": {"project_name": "user1-batch-project"},
        },
        headers=user1["headers"],
    )

    # User2 tries to batch delete User1's plots
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/plots",
        json={"project_name": "user1-batch-project"},
        headers=user2["headers"],
    )

    # Should be forbidden or not found (project exists but user2 has no access)
    assert delete_response.status_code in [
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ]


@pytest.mark.anyio
async def test_delete_plots_by_project_empty_result(client: AsyncClient, dbsession):
    """Test batch delete when no plots match the criteria."""
    user = await create_test_user(client, "plot_batch_empty@test.com")

    # Create project with no plots
    await client.post(
        "/v0/project",
        json={"name": "plot-batch-empty-project"},
        headers=user["headers"],
    )

    # Batch delete on project with no plots
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/plots",
        json={"project_name": "plot-batch-empty-project"},
        headers=user["headers"],
    )

    assert delete_response.status_code == 200
    data = delete_response.json()
    assert data["deleted_count"] == 0


@pytest.mark.anyio
async def test_plot_dao_delete_by_project(client: AsyncClient, dbsession):
    """Test PlotDAO delete_by_project method."""
    user = await create_test_user(client, "plot_dao_delete_project@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_Delete_By_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="PlotDAO_Delete_By_Project")
    project = projects[0][0]

    # Create plots
    plot_dao = PlotDAO(dbsession)
    for i in range(4):
        plot_dao.create(
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
            plot_config={"type": "scatter", "x_axis": f"x{i}", "y_axis": f"y{i}"},
            project_config={"project_name": "PlotDAO_Delete_By_Project"},
            title=f"Plot {i}",
        )
    dbsession.commit()

    # Verify plots exist
    plots = plot_dao.list_by_project(project.id)
    assert len(plots) == 4

    # Delete all plots for project
    deleted_count = plot_dao.delete_by_project(project.id)
    dbsession.commit()

    assert deleted_count == 4

    # Verify plots are gone
    plots = plot_dao.list_by_project(project.id)
    assert len(plots) == 0


@pytest.mark.anyio
async def test_plot_dao_delete_by_project_with_context(client: AsyncClient, dbsession):
    """Test PlotDAO delete_by_project with context filter."""
    user = await create_test_user(client, "plot_dao_delete_ctx@test.com")

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="PlotDAO_Delete_By_Context",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(
        user_id=user["id"],
        name="PlotDAO_Delete_By_Context",
    )
    project = projects[0][0]

    # Create plots with different contexts
    plot_dao = PlotDAO(dbsession)
    for i in range(3):
        plot_dao.create(
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
            plot_config={"type": "scatter", "x_axis": f"x{i}", "y_axis": f"y{i}"},
            project_config={
                "project_name": "PlotDAO_Delete_By_Context",
                "context": "ctx-to-delete",
            },
            title=f"Delete {i}",
        )
    for i in range(2):
        plot_dao.create(
            project_id=project.id,
            user_id=user["id"],
            organization_id=None,
            plot_config={"type": "bar", "x_axis": "model", "y_axis": "count"},
            project_config={
                "project_name": "PlotDAO_Delete_By_Context",
                "context": "ctx-to-keep",
            },
            title=f"Keep {i}",
        )
    dbsession.commit()

    # Verify all plots exist
    all_plots = plot_dao.list_by_project(project.id)
    assert len(all_plots) == 5

    # Delete only ctx-to-delete
    deleted_count = plot_dao.delete_by_project(project.id, context="ctx-to-delete")
    dbsession.commit()

    assert deleted_count == 3

    # Verify only ctx-to-keep plots remain
    remaining_plots = plot_dao.list_by_project(project.id)
    assert len(remaining_plots) == 2
    for plot in remaining_plots:
        assert plot.project_config.get("context") == "ctx-to-keep"
