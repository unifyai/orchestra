"""
Comprehensive performance test suite for RepairsDemo EAV vs JSONB comparison.

This module contains realistic performance tests based on actual RepairsDemo data patterns.
Each test is parametrized to run in both EAV and JSONB modes for direct performance comparison.

RepairsDemo Data Characteristics (from analysis of ~1000 logs):
- WorksOrderStatusDescription: Closed (79%), Issued (14%), Cancelled (<1%)
- WorksOrderPriorityDescription: Routine (66%), Emergency (25%), D&m Stage 1/2 (6%)
- FirstTimeFix: Yes (84%), No (16%)
- Common keywords: electric (23%), leak (19%), door (15%), toilet (9%), window (7%)
- Date range: Sept-Oct 2025
- Operatives: 122 unique names
- Schemes: 493 unique schemes

REQUIREMENTS:
- The local server must be running at localhost:8000
- Projects RepairsAgent_EAV and RepairsAgent_JSONB must exist in the database
- Run with: pytest orchestra/tests/test_log/test_repairs_performance.py -m performance --timeout=300
"""

import json

import pytest

# Set a longer timeout for all tests in this module (5 minutes)
# Performance tests against real data can take a while
pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.performance,
]

from orchestra.conftest import toggle_jsonb_mode

from . import HEADERS


async def setup_mode(client, mode, repairs_eav_project, repairs_jsonb_project):
    """
    Toggle JSONB mode and return the appropriate project name from fixtures.

    :param client: AsyncClient instance
    :param mode: 'eav' or 'jsonb'
    :param repairs_eav_project: Project name from fixture for EAV project
    :param repairs_jsonb_project: Project name from fixture for JSONB project
    :return: Project name string from the appropriate fixture
    """
    enabled = mode == "jsonb"
    await toggle_jsonb_mode(client, enabled, headers=HEADERS)

    if mode == "eav":
        return repairs_eav_project
    else:
        return repairs_jsonb_project


# =============================================================================
# A. BASIC RETRIEVAL (6 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_basic_retrieval_1000(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Test basic log retrieval with limit=1000 (max allowed)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )
    params = {
        "project_name": project,
        "context": repairs_context,
        "limit": 1000,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert "logs" in data


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_basic_retrieval_small(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Test retrieval with small limit (common UI pagination scenario)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_project_scoped_retrieval(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
):
    """Test retrieval scoped to project only (no context filter)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "limit": 500,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# B. STATUS FILTERS - Based on real status distribution (4 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_closed_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter closed jobs with first time fix or routine priority."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderStatusDescription == 'Closed' and (FirstTimeFix == 'Yes' or WorksOrderPriorityDescription == 'Routine')",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_issued_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter issued jobs that are emergency or have access issues."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderStatusDescription == 'Issued' and (WorksOrderPriorityDescription == 'Emergency' or (NoAccess is not None and NoAccess != 'None'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_multiple_statuses(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter by multiple status values with priority and fix constraints."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(WorksOrderStatusDescription in ['Closed', 'Issued', 'Cancelled']) and (WorksOrderPriorityDescription in ['Emergency', 'Routine']) and (FirstTimeFix == 'Yes' or FollowOn == 'No')",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_not_closed(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter open jobs that need urgent attention (not closed + emergency/D&M + failed fix)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderStatusDescription != 'Closed' and ((WorksOrderPriorityDescription == 'Emergency' or WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2']) and (FirstTimeFix == 'No' or FollowOn == 'Yes'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# C. PRIORITY FILTERS - Emergency vs Routine (4 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_emergency_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter emergency jobs that are either closed successfully or still pending with access."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderPriorityDescription == 'Emergency' and ((WorksOrderStatusDescription == 'Closed' and FirstTimeFix == 'Yes') or (WorksOrderStatusDescription == 'Issued' and (NoAccess is None or NoAccess == 'None')))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_routine_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter routine jobs with specific outcomes (closed with fix OR issued with visit recorded)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderPriorityDescription == 'Routine' and ((WorksOrderStatusDescription == 'Closed' and (FirstTimeFix == 'Yes' or SecondTimeFix == 'Yes')) or (WorksOrderStatusDescription == 'Issued' and ArrivedOnSite is not None and ArrivedOnSite != 'NULL'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_damp_mould_priorities(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter D&M priority jobs with keyword match and completion status."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2', 'D & M Survey']) and (('mould' in WorksOrderDescription or 'damp' in WorksOrderDescription or 'condensation' in WorksOrderDescription) or (WorksOrderStatusDescription == 'Closed' and FollowOn == 'No'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_high_priority_or(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter high priority jobs with complex outcome analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "((WorksOrderPriorityDescription == 'Emergency' or WorksOrderPriorityDescription == 'D&m Stage 1') and ((WorksOrderStatusDescription == 'Closed' and FirstTimeFix == 'Yes') or (WorksOrderStatusDescription == 'Issued' and ArrivedOnSite is not None))) or (WorksOrderPriorityDescription == 'D&m Stage 2' and FollowOn == 'Yes')",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# D. FIX SUCCESS FILTERS - FirstTimeFix, FollowOn, SecondTimeFix (5 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_first_time_fix_yes(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter first time fix with full completion chain validation."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "FirstTimeFix == 'Yes' and WorksOrderStatusDescription == 'Closed' and FollowOn == 'No' and (ArrivedOnSite is not None and ArrivedOnSite != 'NULL') and (CompletedVisit is not None and CompletedVisit != 'NULL')",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_failed_first_fix(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter failed first fix with analysis of recovery path."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "FirstTimeFix == 'No' and ((SecondTimeFix == 'Yes' and WorksOrderStatusDescription == 'Closed') or (FollowOn == 'Yes' and WorksOrderStatusDescription in ['Issued', 'Closed']) or (NoAccess is not None and NoAccess != 'None'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_needs_follow_on(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter follow-on jobs with priority and completion analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "FollowOn == 'Yes' and ((WorksOrderPriorityDescription in ['Emergency', 'D&m Stage 1', 'D&m Stage 2'] and WorksOrderStatusDescription != 'Closed') or (FirstTimeFix == 'No' and SecondTimeFix == 'No' and ArrivedOnSite is not None))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_second_time_fix(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter second time fix with full job lifecycle analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "SecondTimeFix == 'Yes' and FirstTimeFix == 'No' and WorksOrderStatusDescription == 'Closed' and ((WorksOrderPriorityDescription == 'Routine' and FollowOn == 'No') or (WorksOrderPriorityDescription in ['Emergency', 'D&m Stage 1'] and CompletedVisit is not None))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_problematic_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter problematic jobs with comprehensive failure analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "FirstTimeFix == 'No' and FollowOn == 'Yes' and ((WorksOrderStatusDescription != 'Closed' and (WorksOrderPriorityDescription == 'Emergency' or WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2'])) or (SecondTimeFix == 'No' and ArrivedOnSite is not None and ArrivedOnSite != 'NULL'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# E. SUBSTRING SEARCH - WorksOrderDescription (6 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_search_electric_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Search for electrical jobs with priority and outcome constraints."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "('electric' in WorksOrderDescription or 'socket' in WorksOrderDescription or 'wiring' in WorksOrderDescription) and ((WorksOrderPriorityDescription == 'Emergency' and WorksOrderStatusDescription == 'Closed') or (FirstTimeFix == 'Yes' and NoAccess is None))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_search_leak_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Search for leak-related jobs with urgency and response analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "('leak' in WorksOrderDescription or 'leaking' in WorksOrderDescription or 'water damage' in WorksOrderDescription) and ((WorksOrderPriorityDescription == 'Emergency' and ArrivedOnSite is not None and ArrivedOnSite != 'NULL') or (WorksOrderStatusDescription == 'Closed' and FirstTimeFix == 'Yes' and FollowOn == 'No'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_search_door_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Search for door-related jobs with security priority analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "('door' in WorksOrderDescription or 'lock' in WorksOrderDescription or 'entry' in WorksOrderDescription) and ((WorksOrderPriorityDescription == 'Emergency' and (FirstTimeFix == 'Yes' or SecondTimeFix == 'Yes')) or (WorksOrderPriorityDescription == 'Routine' and WorksOrderStatusDescription == 'Closed' and FollowOn == 'No'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_search_mould_damp_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Search for mould/damp jobs with D&M priority correlation and outcome tracking."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(('mould' in WorksOrderDescription or 'damp' in WorksOrderDescription or 'condensation' in WorksOrderDescription or 'ventilation' in WorksOrderDescription) and (WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2', 'D & M Survey'])) or (('black spot' in WorksOrderDescription or 'fungus' in WorksOrderDescription) and WorksOrderStatusDescription != 'Cancelled')",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_search_plumbing_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Search for comprehensive plumbing jobs with urgency and completion criteria."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(('sink' in WorksOrderDescription or 'toilet' in WorksOrderDescription or 'pipe' in WorksOrderDescription or 'tap' in WorksOrderDescription or 'drain' in WorksOrderDescription or 'cistern' in WorksOrderDescription) and ((WorksOrderPriorityDescription == 'Emergency' and (FirstTimeFix == 'Yes' or ArrivedOnSite is not None)) or (WorksOrderPriorityDescription == 'Routine' and WorksOrderStatusDescription == 'Closed'))) or (('blocked' in WorksOrderDescription or 'overflow' in WorksOrderDescription) and FollowOn == 'No')",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_search_heating_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Search for heating jobs with seasonal urgency and outcome analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(('heating' in WorksOrderDescription or 'boiler' in WorksOrderDescription or 'radiator' in WorksOrderDescription or 'thermostat' in WorksOrderDescription or 'no hot water' in WorksOrderDescription) and ((WorksOrderPriorityDescription == 'Emergency' and WorksOrderStatusDescription in ['Closed', 'Issued']) or (FirstTimeFix == 'Yes' and CompletedVisit is not None and CompletedVisit != 'NULL'))) or (('gas' in WorksOrderDescription and 'smell' in WorksOrderDescription) and WorksOrderPriorityDescription == 'Emergency')",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# F. ACCESS ISSUES FILTERS (3 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_no_access_issues(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter access issue jobs with priority and rescheduling analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(NoAccess is not None and NoAccess != 'None') and ((WorksOrderPriorityDescription == 'Emergency' and (WorksOrderStatusDescription == 'Issued' or FollowOn == 'Yes')) or (WorksOrderPriorityDescription == 'Routine' and ArrivedOnSite is not None and FirstTimeFix == 'No') or (NoAccess == 'CUSTOMER NOT AT HOME' and WorksOrderStatusDescription != 'Closed'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_customer_not_home(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter customer not home with rescheduling and priority impact analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "NoAccess == 'CUSTOMER NOT AT HOME' and ((WorksOrderPriorityDescription in ['Emergency', 'D&m Stage 1'] and (FollowOn == 'Yes' or WorksOrderStatusDescription == 'Issued')) or (WorksOrderPriorityDescription == 'Routine' and FirstTimeFix == 'No' and ArrivedOnSite is not None and ArrivedOnSite != 'NULL') or (SecondTimeFix == 'Yes' and WorksOrderStatusDescription == 'Closed'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_successful_access(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter successful access jobs with full completion chain validation."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(NoAccess is None or NoAccess == 'None') and ArrivedOnSite is not None and ArrivedOnSite != 'NULL' and ((WorksOrderStatusDescription == 'Closed' and (FirstTimeFix == 'Yes' or SecondTimeFix == 'Yes') and CompletedVisit is not None) or (WorksOrderStatusDescription == 'Issued' and WorksOrderPriorityDescription == 'Emergency' and FollowOn == 'Yes'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# G. DATETIME FILTERS (4 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_arrived_on_site(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter arrived jobs with outcome and priority correlation analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(ArrivedOnSite is not None and ArrivedOnSite != 'NULL') and ((WorksOrderStatusDescription == 'Closed' and FirstTimeFix == 'Yes' and (NoAccess is None or NoAccess == 'None')) or (WorksOrderPriorityDescription == 'Emergency' and (CompletedVisit is not None and CompletedVisit != 'NULL')) or (WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2'] and FollowOn == 'Yes'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_completed_visits(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter completed visits with full lifecycle and success validation."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(CompletedVisit is not None and CompletedVisit != 'NULL') and ArrivedOnSite is not None and ((WorksOrderStatusDescription == 'Closed' and (FirstTimeFix == 'Yes' or SecondTimeFix == 'Yes') and FollowOn == 'No') or (WorksOrderPriorityDescription == 'Emergency' and NoAccess is None and FirstTimeFix == 'Yes') or (WorksOrderPriorityDescription in ['Routine', 'D&m Stage 1'] and WorksOrderStatusDescription == 'Closed'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_date_range_september(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter September 2025 jobs with priority and outcome analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(WorksOrderRaisedDate >= '2025-09-01' and WorksOrderRaisedDate < '2025-10-01') and ((WorksOrderPriorityDescription == 'Emergency' and (WorksOrderStatusDescription == 'Closed' or ArrivedOnSite is not None)) or (WorksOrderPriorityDescription == 'Routine' and FirstTimeFix == 'Yes' and FollowOn == 'No') or (WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2'] and CompletedVisit is not None))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_jobs_not_started(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter unstarted jobs with urgency and backlog analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(ArrivedOnSite is None or ArrivedOnSite == 'NULL') and WorksOrderStatusDescription == 'Issued' and ((WorksOrderPriorityDescription == 'Emergency' and (NoAccess is None or NoAccess == 'None')) or (WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2'] and ('mould' in WorksOrderDescription or 'damp' in WorksOrderDescription)) or (WorksOrderPriorityDescription == 'Routine' and FollowOn == 'No'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# H. OPERATIVE FILTERS - Based on 122 unique operatives (3 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_specific_operative(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter specific operative with performance and workload analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "OperativeName == 'Adrian Hall' and ((WorksOrderStatusDescription == 'Closed' and FirstTimeFix == 'Yes' and FollowOn == 'No') or (WorksOrderPriorityDescription == 'Emergency' and ArrivedOnSite is not None and ArrivedOnSite != 'NULL') or (OperativeName == OperativeWhoCompletedJob and CompletedVisit is not None))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_multiple_operatives(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter multiple operatives with comparative performance analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(OperativeName in ['Adrian Hall', 'Gary Simmons', 'Robert Barker', 'Andrew Cherrington']) and ((FirstTimeFix == 'Yes' and WorksOrderStatusDescription == 'Closed' and FollowOn == 'No') or (WorksOrderPriorityDescription == 'Emergency' and (ArrivedOnSite is not None or NoAccess is not None)) or (OperativeName != OperativeWhoCompletedJob and SecondTimeFix == 'Yes'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_operative_completed_job(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter reassigned jobs with handoff and outcome analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "OperativeName != OperativeWhoCompletedJob and ((WorksOrderStatusDescription == 'Closed' and (FirstTimeFix == 'No' or SecondTimeFix == 'Yes')) or (WorksOrderPriorityDescription == 'Emergency' and ArrivedOnSite is not None and CompletedVisit is not None) or (FollowOn == 'Yes' and NoAccess is None and WorksOrderStatusDescription in ['Closed', 'Issued']))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# I. SCHEME FILTERS - Based on 493 unique schemes (2 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_scheme_startswith(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter scheme prefix with estate performance and priority analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(SchemeName.startswith('COCHRANE') or SchemeName.startswith('ASHFORD')) and ((WorksOrderStatusDescription == 'Closed' and FirstTimeFix == 'Yes') or (WorksOrderPriorityDescription == 'Emergency' and ArrivedOnSite is not None and ArrivedOnSite != 'NULL') or (WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2'] and ('mould' in WorksOrderDescription or 'damp' in WorksOrderDescription)))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_filter_scheme_contains(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter scheme substring with specialist accommodation priority analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(('SPEC ACCOM' in SchemeName or 'SHELTERED' in SchemeName or 'ELDERLY' in SchemeName) and ((WorksOrderPriorityDescription == 'Emergency' and FirstTimeFix == 'Yes') or (WorksOrderStatusDescription == 'Closed' and FollowOn == 'No' and CompletedVisit is not None))) or (('VULNERABLE' in SchemeName or 'SUPPORT' in SchemeName) and ArrivedOnSite is not None and NoAccess is None)",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# J. COMPLEX MULTI-FIELD FILTERS - Business Logic Scenarios (8 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_complex_successful_emergency_repairs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Emergency repairs with full success criteria and response time validation."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderPriorityDescription == 'Emergency' and FirstTimeFix == 'Yes' and WorksOrderStatusDescription == 'Closed' and FollowOn == 'No' and (NoAccess is None or NoAccess == 'None') and ArrivedOnSite is not None and ArrivedOnSite != 'NULL' and CompletedVisit is not None and CompletedVisit != 'NULL' and OperativeName == OperativeWhoCompletedJob",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_complex_failed_routine_repairs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Routine failures with comprehensive recovery path and outcome analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderPriorityDescription == 'Routine' and FirstTimeFix == 'No' and FollowOn == 'Yes' and ((SecondTimeFix == 'Yes' and WorksOrderStatusDescription == 'Closed' and CompletedVisit is not None) or (SecondTimeFix == 'No' and WorksOrderStatusDescription == 'Issued' and ArrivedOnSite is not None) or (NoAccess is not None and NoAccess != 'None' and OperativeName != OperativeWhoCompletedJob))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_complex_emergency_leak_repairs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Emergency leak repairs with critical plumbing response and outcome validation."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderPriorityDescription == 'Emergency' and ('leak' in WorksOrderDescription or 'burst' in WorksOrderDescription or 'flooding' in WorksOrderDescription) and ((FirstTimeFix == 'Yes' and WorksOrderStatusDescription == 'Closed' and NoAccess is None and ArrivedOnSite is not None) or (FirstTimeFix == 'No' and FollowOn == 'Yes' and SecondTimeFix == 'Yes') or (WorksOrderStatusDescription == 'Issued' and ArrivedOnSite is not None and CompletedVisit is None))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_complex_damp_mould_investigation(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """D&M investigation with full workflow tracking and multi-stage analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "(WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2', 'D & M Survey'] and ('mould' in WorksOrderDescription or 'damp' in WorksOrderDescription or 'condensation' in WorksOrderDescription or 'ventilation' in WorksOrderDescription)) and ((WorksOrderStatusDescription == 'Closed' and (FirstTimeFix == 'Yes' or (FollowOn == 'Yes' and SecondTimeFix == 'Yes'))) or (WorksOrderStatusDescription == 'Issued' and ArrivedOnSite is not None and NoAccess is None) or (CompletedVisit is not None and OperativeName == OperativeWhoCompletedJob))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_complex_access_denied_emergency(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Emergency access denied with rescheduling and resolution tracking."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderPriorityDescription == 'Emergency' and NoAccess is not None and NoAccess != 'None' and ((FollowOn == 'Yes' and (SecondTimeFix == 'Yes' or WorksOrderStatusDescription == 'Issued')) or (NoAccess == 'CUSTOMER NOT AT HOME' and ArrivedOnSite is not None) or (WorksOrderStatusDescription == 'Closed' and OperativeName != OperativeWhoCompletedJob))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_complex_pending_high_priority(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """High priority backlog with urgency scoring and access status analysis."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderStatusDescription == 'Issued' and ((WorksOrderPriorityDescription == 'Emergency' and (NoAccess is None or NoAccess == 'None') and (ArrivedOnSite is None or ArrivedOnSite == 'NULL')) or (WorksOrderPriorityDescription in ['D&m Stage 1', 'D&m Stage 2'] and ('mould' in WorksOrderDescription or 'damp' in WorksOrderDescription)) or (FollowOn == 'Yes' and FirstTimeFix == 'No' and SecondTimeFix == 'No'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_complex_multiple_visit_analysis(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Multiple visit analysis with full failure chain and resource tracking."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "FirstTimeFix == 'No' and SecondTimeFix == 'No' and FollowOn == 'Yes' and ((WorksOrderPriorityDescription in ['Emergency', 'D&m Stage 1'] and WorksOrderStatusDescription != 'Cancelled') or (OperativeName != OperativeWhoCompletedJob and ArrivedOnSite is not None and CompletedVisit is not None) or (NoAccess is not None and NoAccess != 'None' and WorksOrderStatusDescription == 'Issued'))",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_complex_electrical_emergency_closed(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Emergency electrical with full success chain and operative performance validation."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderPriorityDescription == 'Emergency' and ('electric' in WorksOrderDescription or 'socket' in WorksOrderDescription or 'power' in WorksOrderDescription or 'wiring' in WorksOrderDescription) and WorksOrderStatusDescription == 'Closed' and FirstTimeFix == 'Yes' and FollowOn == 'No' and (NoAccess is None or NoAccess == 'None') and ArrivedOnSite is not None and ArrivedOnSite != 'NULL' and CompletedVisit is not None and OperativeName == OperativeWhoCompletedJob",
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# K. SORTING TESTS (6 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_by_row_id_desc(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort by row_id descending (most recent first)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "sorting": json.dumps({"row_id": "descending"}),
        "limit": 500,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_by_raised_date_desc(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort by WorksOrderRaisedDate descending (newest jobs first)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "sorting": json.dumps({"WorksOrderRaisedDate": "descending"}),
        "limit": 500,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_by_operative_name_asc(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort by OperativeName ascending (alphabetical)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "sorting": json.dumps({"OperativeName": "ascending"}),
        "limit": 500,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_multi_field(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Multi-field sort: Priority desc, then status asc."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "sorting": json.dumps(
            {
                "WorksOrderPriorityDescription": "descending",
                "WorksOrderStatusDescription": "ascending",
            },
        ),
        "limit": 500,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_filtered_results(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Filter emergency jobs AND sort by raised date."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderPriorityDescription == 'Emergency'",
        "sorting": json.dumps({"WorksOrderRaisedDate": "descending"}),
        "limit": 500,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_by_scheme_name(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort by SchemeName ascending (group jobs by location)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "sorting": json.dumps({"SchemeName": "ascending"}),
        "limit": 500,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# L. SEMANTIC SIMILARITY SORTING (3 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_by_cosine_leak_similarity(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort by semantic similarity to 'water leak under kitchen sink' using cosine."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    # Sort expression using cosine similarity with embedded query
    sort_expr = "cosine(_WorksOrderDescription_emb, embed('water leak under kitchen sink pipes dripping', model='text-embedding-3-small'))"

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "_WorksOrderDescription_emb is not None",
        "sorting": json.dumps({sort_expr: "descending"}),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_by_cosine_electrical_similarity(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort by semantic similarity to 'electrical socket not working power outlet'."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    sort_expr = "cosine(_WorksOrderDescription_emb, embed('electrical socket not working power outlet faulty', model='text-embedding-3-small'))"

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "_WorksOrderDescription_emb is not None",
        "sorting": json.dumps({sort_expr: "descending"}),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sort_by_cosine_mould_similarity(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort by semantic similarity to 'black mould on bathroom ceiling damp'."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    sort_expr = "cosine(_WorksOrderDescription_emb, embed('black mould on bathroom ceiling damp condensation', model='text-embedding-3-small'))"

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "_WorksOrderDescription_emb is not None",
        "sorting": json.dumps({sort_expr: "descending"}),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# M. PAGINATION TESTS (3 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_pagination_first_page(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Pagination: Get first page of 100 results."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "limit": 100,
        "offset": 0,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_pagination_deep_offset(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Pagination: Get results at deep offset (page 50 of 100)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "limit": 100,
        "offset": 5000,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_pagination_with_filter_and_sort(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Pagination with filter and sort (realistic UI scenario)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "filter_expr": "WorksOrderStatusDescription == 'Closed'",
        "sorting": json.dumps({"WorksOrderRaisedDate": "descending"}),
        "limit": 50,
        "offset": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# N. FIELDS LISTING (1 test)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_fields_listing(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """List all available fields for the project/context."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
    }

    response = await timed_client.get("/v0/logs/fields", params=params, headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


# =============================================================================
# O. GROUPED METRICS TESTS (7 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_grouped_metric_count_by_status(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Count metric grouped by WorksOrderStatusDescription."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
        "group_by": "entries/WorksOrderStatusDescription",
    }

    response = await timed_client.get(
        "/v0/logs/metric/count",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_grouped_metric_count_by_priority(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Count metric grouped by WorksOrderPriorityDescription."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
        "group_by": "entries/WorksOrderPriorityDescription",
    }

    response = await timed_client.get(
        "/v0/logs/metric/count",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_grouped_metric_mean_by_status(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Mean of row_id grouped by status (tests aggregation with grouping)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
        "group_by": "entries/WorksOrderStatusDescription",
    }

    response = await timed_client.get(
        "/v0/logs/metric/mean",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_grouped_metric_min_max_by_priority(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Min/max of row_id grouped by priority (batch metrics with grouping)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    # Test min metric
    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
        "group_by": "entries/WorksOrderPriorityDescription",
    }

    response = await timed_client.get(
        "/v0/logs/metric/min",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_grouped_metric_multi_level(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Count metric with multi-level grouping (status + priority)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
            ],
        ),
    }

    response = await timed_client.get(
        "/v0/logs/metric/count",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_grouped_metric_with_filter(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Count metric grouped by status with filter applied."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
        "group_by": "entries/WorksOrderStatusDescription",
        "filter_expr": "FirstTimeFix == 'Yes'",
    }

    response = await timed_client.get(
        "/v0/logs/metric/count",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_grouped_metric_by_operative(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Count metric grouped by OperativeName (high cardinality grouping)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
        "group_by": "entries/OperativeName",
    }

    response = await timed_client.get(
        "/v0/logs/metric/count",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


# =============================================================================
# P. SINGLE-LEVEL GROUPING WITH LOGS (6 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_logs_by_status(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group logs by WorksOrderStatusDescription with full log data."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_logs_by_priority(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group logs by WorksOrderPriorityDescription with full log data."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderPriorityDescription"]),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_logs_by_first_time_fix(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group logs by FirstTimeFix (Yes/No)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/FirstTimeFix"]),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_logs_by_operative(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group logs by OperativeName (high cardinality)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/OperativeName"]),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_logs_by_follow_on(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group logs by FollowOn status."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/FollowOn"]),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_logs_by_second_time_fix(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group logs by SecondTimeFix status."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/SecondTimeFix"]),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# Q. MULTI-LEVEL GROUPING (5 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_multi_level_group_status_priority(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Multi-level grouping: Status -> Priority."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
            ],
        ),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_multi_level_group_status_firstfix(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Multi-level grouping: Status -> FirstTimeFix."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            ["entries/WorksOrderStatusDescription", "entries/FirstTimeFix"],
        ),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_multi_level_group_priority_followon(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Multi-level grouping: Priority -> FollowOn."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            ["entries/WorksOrderPriorityDescription", "entries/FollowOn"],
        ),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_multi_level_group_three_levels(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Three-level grouping: Status -> Priority -> FirstTimeFix."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
                "entries/FirstTimeFix",
            ],
        ),
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_multi_level_group_fix_outcome(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Multi-level grouping by fix outcomes: FirstTimeFix -> SecondTimeFix."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/FirstTimeFix", "entries/SecondTimeFix"]),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# R. GROUP PAGINATION (4 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_pagination_first_page(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group pagination: First page of groups (offset=0, limit=3)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderPriorityDescription"]),
        "group_offset": 0,
        "group_limit": 3,
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_pagination_second_page(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group pagination: Second page of groups (offset=3, limit=3)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderPriorityDescription"]),
        "group_offset": 3,
        "group_limit": 3,
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_pagination_operative_groups(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group pagination on high-cardinality OperativeName (limit=10)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/OperativeName"]),
        "group_offset": 0,
        "group_limit": 10,
        "limit": 10,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_pagination_deep_offset(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group pagination with deep offset on OperativeName."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/OperativeName"]),
        "group_offset": 50,
        "group_limit": 10,
        "limit": 10,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# S. GROUP DEPTH (4 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_depth_zero(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group depth=0: Return only counts at each level."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
            ],
        ),
        "group_depth": 0,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_depth_one(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group depth=1: Expand first level only."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
            ],
        ),
        "group_depth": 1,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_depth_two(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group depth=2: Expand two levels."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
                "entries/FirstTimeFix",
            ],
        ),
        "group_depth": 2,
        "limit": 20,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_depth_full(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group depth=3: Full expansion with logs at leaves."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
                "entries/FirstTimeFix",
            ],
        ),
        "group_depth": 3,
        "limit": 10,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# T. GROUP SORTING (5 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_sorting_by_count_desc(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort groups by count (descending) - most common statuses first."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    group_sorting = {
        "entries/WorksOrderStatusDescription": {
            "field": "row_id",
            "metric": "count",
            "direction": "descending",
        },
    }

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "group_sorting": json.dumps(group_sorting),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_sorting_by_count_asc(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort groups by count (ascending) - rarest priorities first."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    group_sorting = {
        "entries/WorksOrderPriorityDescription": {
            "field": "row_id",
            "metric": "count",
            "direction": "ascending",
        },
    }

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderPriorityDescription"]),
        "group_sorting": json.dumps(group_sorting),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_sorting_by_mean_row_id(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort groups by mean row_id (descending) - newer jobs first."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    group_sorting = {
        "entries/WorksOrderStatusDescription": {
            "field": "row_id",
            "metric": "mean",
            "direction": "descending",
        },
    }

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "group_sorting": json.dumps(group_sorting),
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_sorting_multi_level(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Multi-level group sorting: status by count, priority by count."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    group_sorting = {
        "entries/WorksOrderStatusDescription": {
            "field": "row_id",
            "metric": "count",
            "direction": "descending",
        },
        "entries/WorksOrderPriorityDescription": {
            "field": "row_id",
            "metric": "count",
            "direction": "descending",
        },
    }

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
            ],
        ),
        "group_sorting": json.dumps(group_sorting),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_sorting_operative_by_jobs(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort operatives by job count (busiest operatives first)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    group_sorting = {
        "entries/OperativeName": {
            "field": "row_id",
            "metric": "count",
            "direction": "descending",
        },
    }

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/OperativeName"]),
        "group_sorting": json.dumps(group_sorting),
        "group_limit": 20,
        "limit": 10,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# U. SORTING WITHIN GROUPS (4 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sorting_within_groups_by_row_id(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort logs within each status group by row_id descending."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "sorting": json.dumps({"row_id": "descending"}),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sorting_within_groups_by_date(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort logs within each priority group by date ascending."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderPriorityDescription"]),
        "sorting": json.dumps({"WorksOrderRaisedDate": "ascending"}),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sorting_within_groups_by_operative(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sort logs within each status group by OperativeName alphabetically."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "sorting": json.dumps({"OperativeName": "ascending"}),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_sorting_and_group_sorting_combined(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Combine group sorting (by count) and within-group sorting (by row_id)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    group_sorting = {
        "entries/WorksOrderStatusDescription": {
            "field": "row_id",
            "metric": "count",
            "direction": "descending",
        },
    }

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "group_sorting": json.dumps(group_sorting),
        "sorting": json.dumps({"row_id": "descending"}),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# V. COMBINED GROUPING + FILTERING (6 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_with_status_filter(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group by priority with status filter (Closed only)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderPriorityDescription"]),
        "filter_expr": "WorksOrderStatusDescription == 'Closed'",
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_with_first_time_fix_filter(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group by status with FirstTimeFix='Yes' filter."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "filter_expr": "FirstTimeFix == 'Yes'",
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_with_complex_filter(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group by operative with complex filter (Emergency + Closed + FirstTimeFix)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/OperativeName"]),
        "filter_expr": "WorksOrderPriorityDescription == 'Emergency' and WorksOrderStatusDescription == 'Closed' and FirstTimeFix == 'Yes'",
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_with_substring_filter(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group by status with substring filter (leak in description)."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "filter_expr": "'leak' in WorksOrderDescription",
        "limit": 100,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_with_multi_filter_and_sort(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Group by priority with filter, group sorting, and within-group sorting."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    group_sorting = {
        "entries/WorksOrderPriorityDescription": {
            "field": "row_id",
            "metric": "count",
            "direction": "descending",
        },
    }

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderPriorityDescription"]),
        "filter_expr": "WorksOrderStatusDescription == 'Closed'",
        "group_sorting": json.dumps(group_sorting),
        "sorting": json.dumps({"row_id": "descending"}),
        "limit": 50,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_group_multi_level_with_filter(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Multi-level grouping (Status -> Priority) with FirstTimeFix filter."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
            ],
        ),
        "filter_expr": "FirstTimeFix == 'Yes' and FollowOn == 'No'",
        "limit": 30,
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# W. GROUPS ONLY (4 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_groups_only_by_status(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Groups only mode: Return only IDs grouped by status."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "groups_only": "true",
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_groups_only_by_priority(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Groups only mode: Return only IDs grouped by priority."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderPriorityDescription"]),
        "groups_only": "true",
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_groups_only_with_timestamps(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Groups only mode with timestamps: Return ID -> timestamp mapping."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(["entries/WorksOrderStatusDescription"]),
        "groups_only": "true",
        "return_timestamps": "true",
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_groups_only_multi_level(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Groups only mode with multi-level grouping."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "group_by": json.dumps(
            [
                "entries/WorksOrderStatusDescription",
                "entries/WorksOrderPriorityDescription",
            ],
        ),
        "groups_only": "true",
    }

    response = await timed_client.get("/v0/logs", params=params, headers=HEADERS)
    assert response.status_code == 200


# =============================================================================
# X. ADDITIONAL REDUCTION METRICS (6 tests)
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_metric_sum_row_id(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Sum metric on row_id field."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
    }

    response = await timed_client.get(
        "/v0/logs/metric/sum",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_metric_mean_row_id(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Mean metric on row_id field."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
    }

    response = await timed_client.get(
        "/v0/logs/metric/mean",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_metric_min_row_id(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Min metric on row_id field."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
    }

    response = await timed_client.get(
        "/v0/logs/metric/min",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_metric_max_row_id(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Max metric on row_id field."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
    }

    response = await timed_client.get(
        "/v0/logs/metric/max",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_metric_median_row_id(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Median metric on row_id field."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
    }

    response = await timed_client.get(
        "/v0/logs/metric/median",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["eav", "jsonb"])
@pytest.mark.usefixtures("large_repairs_dataset")
@pytest.mark.performance
async def test_metric_std_row_id(
    mode,
    timed_client,
    repairs_eav_project,
    repairs_jsonb_project,
    repairs_context,
):
    """Standard deviation metric on row_id field."""
    project = await setup_mode(
        timed_client,
        mode,
        repairs_eav_project,
        repairs_jsonb_project,
    )

    params = {
        "project_name": project,
        "context": repairs_context,
        "key": "row_id",
    }

    response = await timed_client.get(
        "/v0/logs/metric/std",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
