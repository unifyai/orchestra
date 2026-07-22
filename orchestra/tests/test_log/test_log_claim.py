"""Tests for the atomic claim (compare-and-set) endpoint."""

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


async def _claim(client: AsyncClient, payload: dict):
    return await client.post("/v0/logs/claim", json=payload, headers=HEADERS)


@pytest.mark.anyio
async def test_claim_single_row(client: AsyncClient):
    project_name = "claim-single-test"
    await _create_project(client, project_name)

    response = await _create_log(
        client,
        project_name,
        entries={"job_id": "job-1", "status": "queued"},
        context="Jobs",
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    response = await _claim(
        client,
        {
            "project": project_name,
            "context": "Jobs",
            "expect": {"job_id": "job-1", "status": "queued"},
            "updates": {"status": "processing"},
        },
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["count"] == 1
    assert body["claimed"][0]["id"] == log_id
    assert body["claimed"][0]["data"]["status"] == "processing"

    # A second claim on the same expectation must find nothing.
    response = await _claim(
        client,
        {
            "project": project_name,
            "context": "Jobs",
            "expect": {"job_id": "job-1", "status": "queued"},
            "updates": {"status": "processing"},
        },
    )
    assert response.status_code == 200, response.json()
    assert response.json()["count"] == 0


@pytest.mark.anyio
async def test_claim_respects_expect_and_context(client: AsyncClient):
    project_name = "claim-scope-test"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries={"job_id": "job-a", "status": "sent"},
        context="Jobs",
    )
    await _create_log(
        client,
        project_name,
        entries={"job_id": "job-a", "status": "queued"},
        context="OtherJobs",
    )

    # Wrong status: nothing claimable.
    response = await _claim(
        client,
        {
            "project": project_name,
            "context": "Jobs",
            "expect": {"job_id": "job-a", "status": "queued"},
            "updates": {"status": "processing"},
        },
    )
    assert response.status_code == 200, response.json()
    assert response.json()["count"] == 0

    # Row in a different context must not be visible either.
    response = await _claim(
        client,
        {
            "project": project_name,
            "context": "Jobs",
            "expect": {"job_id": "job-a"},
            "updates": {"status": "processing"},
        },
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1  # only the Jobs-context row


@pytest.mark.anyio
async def test_claim_numeric_and_bool_expect(client: AsyncClient):
    project_name = "claim-typed-test"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries={"campaign_id": 42, "enabled": True, "status": "queued"},
        context="Jobs",
    )

    response = await _claim(
        client,
        {
            "project": project_name,
            "context": "Jobs",
            "expect": {"campaign_id": 42, "enabled": True, "status": "queued"},
            "updates": {"status": "processing", "attempts": 1},
        },
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["count"] == 1
    assert body["claimed"][0]["data"]["attempts"] == 1


@pytest.mark.anyio
async def test_claim_limit_batches(client: AsyncClient):
    project_name = "claim-limit-test"
    await _create_project(client, project_name)

    for index in range(3):
        await _create_log(
            client,
            project_name,
            entries={"job_id": f"job-{index}", "status": "queued"},
            context="Jobs",
        )

    response = await _claim(
        client,
        {
            "project": project_name,
            "context": "Jobs",
            "expect": {"status": "queued"},
            "updates": {"status": "processing"},
            "limit": 2,
        },
    )
    assert response.status_code == 200, response.json()
    assert response.json()["count"] == 2

    response = await _claim(
        client,
        {
            "project": project_name,
            "context": "Jobs",
            "expect": {"status": "queued"},
            "updates": {"status": "processing"},
            "limit": 2,
        },
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1


@pytest.mark.anyio
async def test_claim_rejects_empty_expect(client: AsyncClient):
    project_name = "claim-validation-test"
    await _create_project(client, project_name)

    response = await _claim(
        client,
        {
            "project": project_name,
            "context": "Jobs",
            "expect": {},
            "updates": {"status": "processing"},
        },
    )
    assert response.status_code == 400
