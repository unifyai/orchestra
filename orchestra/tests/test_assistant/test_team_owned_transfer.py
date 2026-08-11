"""Tests for converting org assistants to team-owned scope."""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import text

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.models.core_models import Context
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_TEAM,
    Assistant,
    ContactMembership,
)
from orchestra.tests.utils import create_test_user, ensure_assistants_project

_SHARED_TEAM_SHELLS = (
    "Contacts",
    "Guidance",
    "Knowledge",
    "Secrets",
    "Transcripts",
)


async def _create_org_with_team(
    client: AsyncClient,
    owner,
    *,
    org_name: str,
    team_name: str = "Automation",
) -> tuple[int, dict, int]:
    org_response = await client.post(
        "/v0/organizations",
        json={"name": org_name, "data_sharing_mode": "private"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED, org_response.json()
    org_id = org_response.json()["id"]
    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_response.json()['api_key']}",
    }
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": team_name},
        headers=owner["headers"],
    )
    assert team_response.status_code == status.HTTP_201_CREATED, team_response.json()
    return org_id, org_headers, team_response.json()["id"]


async def _hire_org_assistant(
    client: AsyncClient,
    org_headers: dict,
    *,
    first_name: str = "Brain",
) -> dict:
    response = await client.post(
        "/v0/assistant",
        json={
            "first_name": first_name,
            "surname": "Operator",
            "create_infra": False,
            "is_local": True,
        },
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    return response.json()["info"]


def _assistants_project_id(dbsession, org_id: int) -> int:
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    org_projects = project_dao.filter(
        organization_id=org_id,
        name="Assistants",
    )
    assert org_projects
    return org_projects[0][0].id


def _seed_empty_team_shells(
    dbsession,
    *,
    project_id: int,
    team_id: int,
) -> None:
    context_dao = ContextDAO(dbsession)
    for table_name in _SHARED_TEAM_SHELLS:
        context_dao.create(project_id, f"Teams/{team_id}/{table_name}")
    dbsession.commit()


async def _write_personal_logs(
    client: AsyncClient,
    org_headers: dict,
    *,
    personal_prefix: str,
    contexts: list[str],
) -> None:
    for context_suffix in contexts:
        context_name = f"{personal_prefix}/{context_suffix}"
        log_resp = await client.post(
            "/v0/logs",
            json={
                "project_name": "Assistants",
                "context": context_name,
                "entries": [{"message": f"log in {context_suffix}"}],
            },
            headers=org_headers,
        )
        assert log_resp.status_code == status.HTTP_200_OK, log_resp.json()


def _seed_table(
    dbsession,
    project_id: int,
    name: str,
    *,
    unique_keys: dict | None = None,
    auto_counting: dict | None = None,
    foreign_keys: list | None = None,
    versioned: bool = False,
) -> None:
    """Create a schema-bearing context, or align an existing one's schema."""
    context_dao = ContextDAO(dbsession)
    existing = context_dao.filter(project_id=project_id, name=name)
    if existing:
        context = existing[0][0]
        context.unique_key_names = list(unique_keys.keys()) if unique_keys else []
        context.unique_key_types = list(unique_keys.values()) if unique_keys else []
        context.auto_counting = auto_counting or {}
        context.foreign_keys = foreign_keys or []
        context.is_versioned = versioned
        dbsession.add(context)
    else:
        context_dao.create(
            project_id,
            name,
            unique_keys=unique_keys,
            auto_counting=auto_counting,
            foreign_keys=foreign_keys,
            is_versioned=versioned,
        )
    dbsession.commit()


async def _post_rows(
    client: AsyncClient,
    org_headers: dict,
    context_name: str,
    entries: list[dict],
) -> list[int]:
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": "Assistants",
            "context": context_name,
            "entries": entries,
        },
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    return response.json()["log_event_ids"]


def _table_rows(dbsession, project_id: int, context_name: str) -> list[dict]:
    rows = dbsession.execute(
        text(
            """
            SELECT le.data
            FROM log_event le
            JOIN log_event_context lec
              ON lec.log_event_id = le.id
             AND lec.project_id = le.project_id
            JOIN context c ON c.id = lec.context_id
            WHERE le.project_id = :project_id
              AND c.project_id = :project_id
              AND c.name = :context_name
            ORDER BY le.id
            """,
        ),
        {"project_id": project_id, "context_name": context_name},
    ).fetchall()
    return [row[0] for row in rows]


async def _setup_merge_org(
    client: AsyncClient,
    dbsession,
    *,
    email: str,
    org_name: str,
) -> tuple[int, dict, int, int, int, str]:
    """Org + team + hired assistant, returning ids and the personal prefix."""
    owner = await create_test_user(client, email)
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name=org_name,
    )
    await ensure_assistants_project(client, org_headers)
    info = await _hire_org_assistant(client, org_headers)
    agent_id = int(info["agent_id"])
    dbsession.expire_all()
    assistant = dbsession.get(Assistant, agent_id)
    personal_prefix = f"{assistant.user_id}/{agent_id}"
    project_id = _assistants_project_id(dbsession, org_id)
    return org_id, org_headers, team_id, agent_id, project_id, personal_prefix


@pytest.mark.anyio
async def test_transfer_org_assistant_to_team_owned(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "team_owned_transfer_owner@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Transfer Org",
    )
    info = await _hire_org_assistant(client, org_headers)
    agent_id = int(info["agent_id"])
    personal_prefix = f"{owner['id']}/{agent_id}"

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    body = response.json()["info"]
    assert body["owner_team_id"] == team_id
    assert body["memory_root"] == f"Teams/{team_id}"

    dbsession.expire_all()
    assistant = dbsession.get(Assistant, agent_id)
    assert assistant.owner_team_id == team_id

    personal_contexts = (
        dbsession.query(Context)
        .filter(Context.name.like(f"{personal_prefix}%"))
        .count()
    )
    assert personal_contexts == 0

    team_contexts = (
        dbsession.query(Context).filter(Context.name.like(f"Teams/{team_id}%")).count()
    )
    assert team_contexts >= 0

    personal_overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        )
        .count()
    )
    assert personal_overlays == 0

    team_overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_TEAM,
            ContactMembership.target_team_id == team_id,
        )
        .count()
    )
    assert team_overlays >= 1


@pytest.mark.anyio
async def test_transfer_succeeds_when_team_has_empty_shared_shells(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "team_owned_empty_shells@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Empty Shells Org",
    )
    await ensure_assistants_project(client, org_headers)
    info = await _hire_org_assistant(client, org_headers)
    agent_id = int(info["agent_id"])
    dbsession.expire_all()
    assistant = dbsession.get(Assistant, agent_id)
    personal_prefix = f"{assistant.user_id}/{agent_id}"

    project_id = _assistants_project_id(dbsession, org_id)
    context_dao = ContextDAO(dbsession)
    _seed_empty_team_shells(dbsession, project_id=project_id, team_id=team_id)
    await _write_personal_logs(
        client,
        org_headers,
        personal_prefix=personal_prefix,
        contexts=["Contacts", "Tasks"],
    )
    dbsession.expire_all()
    assert context_dao.subtree_has_logs(
        project_id,
        f"{personal_prefix}/Contacts",
    )
    assert context_dao.subtree_has_logs(project_id, f"{personal_prefix}/Tasks")

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    personal_contexts = (
        dbsession.query(Context)
        .filter(Context.name.like(f"{personal_prefix}%"))
        .count()
    )
    assert personal_contexts == 0

    dbsession.expire_all()
    assert context_dao.subtree_has_logs(project_id, f"Teams/{team_id}/Contacts")
    assert context_dao.subtree_has_logs(project_id, f"Teams/{team_id}/Tasks")


@pytest.mark.anyio
async def test_transfer_rejects_both_sides_have_data(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "team_owned_collision@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Collision Org",
    )
    await ensure_assistants_project(client, org_headers)
    info = await _hire_org_assistant(client, org_headers)
    agent_id = int(info["agent_id"])
    dbsession.expire_all()
    assistant = dbsession.get(Assistant, agent_id)
    personal_prefix = f"{assistant.user_id}/{agent_id}"

    project_id = _assistants_project_id(dbsession, org_id)
    _seed_empty_team_shells(dbsession, project_id=project_id, team_id=team_id)

    await _write_personal_logs(
        client,
        org_headers,
        personal_prefix=personal_prefix,
        contexts=["Contacts"],
    )
    team_contacts = f"Teams/{team_id}/Contacts"
    team_log_resp = await client.post(
        "/v0/logs",
        json={
            "project_name": "Assistants",
            "context": team_contacts,
            "entries": [{"message": "team contact log"}],
        },
        headers=org_headers,
    )
    assert team_log_resp.status_code == status.HTTP_200_OK, team_log_resp.json()

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.json()
    assert response.json()["detail"].startswith("team_memory_collision_both_have_data")


_CONTACTS_SCHEMA = {
    "unique_keys": {"contact_id": "int"},
    "auto_counting": {"contact_id": None},
}
# NOT the current Tasks schema. Tasks is now definition-only, keyed by
# ``task_id`` alone, with runs in ``Tasks/Executions``. This fixture keeps a
# parent-scoped auto-counting shape (child counter scoped to a parent id) so
# the merge remap stays covered for contexts declared that way — including
# pre-migration Tasks contexts still in the wild.
_LEGACY_PARENT_SCOPED_SCHEMA = {
    "unique_keys": {"task_id": "int", "instance_id": "int"},
    "auto_counting": {"task_id": None, "instance_id": "task_id"},
}
_SECRETS_SCHEMA = {
    "unique_keys": {"secret_id": "int", "name": "str"},
    "auto_counting": {"secret_id": None},
}
_FUNCTIONS_SCHEMA = {
    "unique_keys": {"function_id": "int"},
    "auto_counting": {"function_id": None},
}


@pytest.mark.anyio
async def test_merge_contacts_when_both_populated(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_contacts@test.com",
        org_name="Team Owned Merge Contacts Org",
    )
    personal_contacts = f"{personal_prefix}/Contacts"
    team_contacts = f"Teams/{team_id}/Contacts"
    # Hire bootstrap already created the personal Contacts with the boss row
    # at contact_id 1; add one more explicit row above it.
    await _post_rows(
        client,
        org_headers,
        personal_contacts,
        [{"contact_id": 5, "first_name": "PersonalPal"}],
    )
    _seed_table(dbsession, project_id, team_contacts, **_CONTACTS_SCHEMA)
    await _post_rows(
        client,
        org_headers,
        team_contacts,
        [
            {"contact_id": 0, "first_name": "TeamSelf"},
            {"contact_id": 1, "first_name": "TeamBoss"},
            {"contact_id": 2, "first_name": "TeamPal"},
        ],
    )

    dbsession.expire_all()
    personal_ids = {
        row["contact_id"]
        for row in _table_rows(dbsession, project_id, personal_contacts)
    }
    assert personal_ids == {1, 5}

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    body = response.json()["info"]
    assert body["contexts_merged"] == 1

    dbsession.expire_all()
    personal_contexts = (
        dbsession.query(Context)
        .filter(Context.name.like(f"{personal_prefix}%"))
        .count()
    )
    assert personal_contexts == 0

    # Team max contact_id was 2, so the personal book shifted by 3.
    team_rows = _table_rows(dbsession, project_id, team_contacts)
    assert {row["contact_id"] for row in team_rows} == {0, 1, 2, 4, 8}
    by_id = {row["contact_id"]: row for row in team_rows}
    assert by_id[8]["first_name"] == "PersonalPal"

    # Counter resync proof: a fresh auto-counted row lands above every
    # merged value instead of colliding with one.
    await _post_rows(
        client,
        org_headers,
        team_contacts,
        [{"first_name": "PostMerge"}],
    )
    dbsession.expire_all()
    post_merge_row = next(
        row
        for row in _table_rows(dbsession, project_id, team_contacts)
        if row["first_name"] == "PostMerge"
    )
    assert post_merge_row["contact_id"] == 9


@pytest.mark.anyio
async def test_merge_tasks_preserves_instance_grouping(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_tasks@test.com",
        org_name="Team Owned Merge Tasks Org",
    )
    personal_tasks = f"{personal_prefix}/Tasks"
    team_tasks = f"Teams/{team_id}/Tasks"
    _seed_table(dbsession, project_id, personal_tasks, **_LEGACY_PARENT_SCOPED_SCHEMA)
    _seed_table(dbsession, project_id, team_tasks, **_LEGACY_PARENT_SCOPED_SCHEMA)
    await _post_rows(
        client,
        org_headers,
        personal_tasks,
        [
            {"task_id": 0, "instance_id": 0, "objective": "personal a"},
            {"task_id": 0, "instance_id": 1, "objective": "personal a clone"},
            {"task_id": 1, "instance_id": 0, "objective": "personal b"},
        ],
    )
    await _post_rows(
        client,
        org_headers,
        team_tasks,
        [{"task_id": 0, "instance_id": 0, "objective": "team a"}],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    team_rows = _table_rows(dbsession, project_id, team_tasks)
    composites = {(row["task_id"], row["instance_id"]) for row in team_rows}
    # Personal task_ids shifted by 1 (team max was 0); the per-task
    # instance grouping is preserved untouched.
    assert composites == {(0, 0), (1, 0), (1, 1), (2, 0)}
    by_composite = {
        (row["task_id"], row["instance_id"]): row["objective"] for row in team_rows
    }
    assert by_composite[(0, 0)] == "team a"
    assert by_composite[(1, 0)] == "personal a"
    assert by_composite[(1, 1)] == "personal a clone"
    assert by_composite[(2, 0)] == "personal b"


@pytest.mark.anyio
async def test_merge_propagates_foreign_key_references(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_fk@test.com",
        org_name="Team Owned Merge FK Org",
    )
    personal_contacts = f"{personal_prefix}/Contacts"
    personal_transcripts = f"{personal_prefix}/Transcripts"
    team_contacts = f"Teams/{team_id}/Contacts"
    team_transcripts = f"Teams/{team_id}/Transcripts"

    await _post_rows(
        client,
        org_headers,
        personal_contacts,
        [{"contact_id": 2, "first_name": "Pal"}],
    )
    _seed_table(
        dbsession,
        project_id,
        personal_transcripts,
        unique_keys={"message_id": "int"},
        auto_counting={"message_id": None},
        foreign_keys=[
            {
                "name": "sender_id",
                "references": f"{personal_contacts}.contact_id",
                "on_delete": "SET NULL",
                "on_update": "CASCADE",
            },
            {
                "name": "receiver_ids[*]",
                "references": f"{personal_contacts}.contact_id",
                "on_delete": "SET NULL",
                "on_update": "CASCADE",
            },
        ],
    )
    await _post_rows(
        client,
        org_headers,
        personal_transcripts,
        [
            {
                "message_id": 0,
                "sender_id": 2,
                "receiver_ids": [1, 2],
                "message": "hello",
            },
        ],
    )
    _seed_table(dbsession, project_id, team_contacts, **_CONTACTS_SCHEMA)
    await _post_rows(
        client,
        org_headers,
        team_contacts,
        [{"contact_id": 0, "first_name": "TeamSelf"}],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    # Contacts merged (offset 1: team max was 0); Transcripts was renamed and
    # its contact references shifted with the remap, arrays included.
    team_contact_ids = {
        row["contact_id"] for row in _table_rows(dbsession, project_id, team_contacts)
    }
    assert team_contact_ids == {0, 2, 3}
    transcript_rows = _table_rows(dbsession, project_id, team_transcripts)
    assert len(transcript_rows) == 1
    assert transcript_rows[0]["sender_id"] == 3
    assert transcript_rows[0]["receiver_ids"] == [2, 3]

    # FK reference paths were re-rooted from the personal tree to the team's.
    transcripts_context = (
        dbsession.query(Context)
        .filter(
            Context.project_id == project_id,
            Context.name == team_transcripts,
        )
        .one()
    )
    references = {fk["references"] for fk in transcripts_context.foreign_keys}
    assert references == {f"{team_contacts}.contact_id"}


@pytest.mark.anyio
async def test_merge_secrets_dedupes_identical_values(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_secrets@test.com",
        org_name="Team Owned Merge Secrets Org",
    )
    personal_secrets = f"{personal_prefix}/Secrets"
    team_secrets = f"Teams/{team_id}/Secrets"
    _seed_table(dbsession, project_id, personal_secrets, **_SECRETS_SCHEMA)
    _seed_table(dbsession, project_id, team_secrets, **_SECRETS_SCHEMA)
    await _post_rows(
        client,
        org_headers,
        personal_secrets,
        [
            {"secret_id": 0, "name": "shared-key", "value": "same-value"},
            {"secret_id": 1, "name": "personal-key", "value": "personal-value"},
        ],
    )
    await _post_rows(
        client,
        org_headers,
        team_secrets,
        [
            {"secret_id": 0, "name": "shared-key", "value": "same-value"},
            {"secret_id": 1, "name": "team-key", "value": "team-value"},
        ],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    team_rows = _table_rows(dbsession, project_id, team_secrets)
    by_name = {row["name"]: row for row in team_rows}
    assert set(by_name) == {"shared-key", "team-key", "personal-key"}
    # The identical shared secret deduped to the team's copy; the unique
    # personal secret moved with its id shifted above the team max.
    assert by_name["shared-key"]["secret_id"] == 0
    assert by_name["personal-key"]["secret_id"] == 3


@pytest.mark.anyio
async def test_merge_rejects_conflicting_secret_values(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_secret_conflict@test.com",
        org_name="Team Owned Merge Secret Conflict Org",
    )
    personal_secrets = f"{personal_prefix}/Secrets"
    team_secrets = f"Teams/{team_id}/Secrets"
    _seed_table(dbsession, project_id, personal_secrets, **_SECRETS_SCHEMA)
    _seed_table(dbsession, project_id, team_secrets, **_SECRETS_SCHEMA)
    await _post_rows(
        client,
        org_headers,
        personal_secrets,
        [{"secret_id": 0, "name": "shared-key", "value": "personal-value"}],
    )
    await _post_rows(
        client,
        org_headers,
        team_secrets,
        [{"secret_id": 0, "name": "shared-key", "value": "team-value"}],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.json()
    assert response.json()["detail"].startswith("team_memory_merge_secret_conflict")


@pytest.mark.anyio
async def test_merge_rejects_schema_mismatch(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        _personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_mismatch@test.com",
        org_name="Team Owned Merge Mismatch Org",
    )
    # The personal Contacts (hire bootstrap, keyed by contact_id) already has
    # the boss row; give the team table an incompatible key scheme.
    team_contacts = f"Teams/{team_id}/Contacts"
    _seed_table(
        dbsession,
        project_id,
        team_contacts,
        unique_keys={"other_id": "int"},
        auto_counting={"other_id": None},
    )
    await _post_rows(
        client,
        org_headers,
        team_contacts,
        [{"other_id": 0, "first_name": "TeamRow"}],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.json()
    assert response.json()["detail"].startswith("team_memory_merge_schema_mismatch")


@pytest.mark.anyio
async def test_merge_dedupes_identical_functions_and_remaps_references(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_functions@test.com",
        org_name="Team Owned Merge Functions Org",
    )
    personal_functions = f"{personal_prefix}/Functions/Compositional"
    team_functions = f"Teams/{team_id}/Functions/Compositional"
    _seed_table(dbsession, project_id, personal_functions, **_FUNCTIONS_SCHEMA)
    _seed_table(dbsession, project_id, team_functions, **_FUNCTIONS_SCHEMA)

    shared_impl = "def send_digest():\n    return 'digest'"
    await _post_rows(
        client,
        org_headers,
        personal_functions,
        [
            {
                "function_id": 0,
                "name": "helper",
                "implementation": "def helper():\n    return 1",
                "argspec": "()",
                "language": "python",
            },
            {
                "function_id": 1,
                "name": "send_digest",
                "implementation": shared_impl,
                "argspec": "()",
                "language": "python",
            },
        ],
    )
    await _post_rows(
        client,
        org_headers,
        team_functions,
        [
            {
                "function_id": 0,
                "name": "send_digest",
                "implementation": shared_impl,
                "argspec": "()",
                "language": "python",
            },
            {
                "function_id": 1,
                "name": "team_only",
                "implementation": "def team_only():\n    return 2",
                "argspec": "()",
                "language": "python",
            },
        ],
    )
    # A personal task pinned to the personal send_digest via a declared FK.
    personal_tasks = f"{personal_prefix}/Tasks"
    _seed_table(
        dbsession,
        project_id,
        personal_tasks,
        **_LEGACY_PARENT_SCOPED_SCHEMA,
        foreign_keys=[
            {
                "name": "entrypoint",
                "references": f"{personal_functions}.function_id",
                "on_delete": "SET NULL",
                "on_update": "CASCADE",
            },
        ],
    )
    await _post_rows(
        client,
        org_headers,
        personal_tasks,
        [
            {
                "task_id": 0,
                "instance_id": 0,
                "name": "Digest task",
                "entrypoint": 1,
            },
        ],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    # The identical send_digest deduped to the team row (id 0); the unique
    # helper moved with its id shifted above the team max (1 -> offset 2).
    team_rows = _table_rows(dbsession, project_id, team_functions)
    by_name = {row["name"]: row["function_id"] for row in team_rows}
    assert by_name == {"send_digest": 0, "team_only": 1, "helper": 2}

    # The task's entrypoint followed the dedup mapping, not the offset.
    task_rows = _table_rows(dbsession, project_id, f"Teams/{team_id}/Tasks")
    assert len(task_rows) == 1
    assert task_rows[0]["entrypoint"] == 0


@pytest.mark.anyio
async def test_merge_rejects_conflicting_function_definitions(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_fn_conflict@test.com",
        org_name="Team Owned Merge Function Conflict Org",
    )
    personal_functions = f"{personal_prefix}/Functions/Compositional"
    team_functions = f"Teams/{team_id}/Functions/Compositional"
    _seed_table(dbsession, project_id, personal_functions, **_FUNCTIONS_SCHEMA)
    _seed_table(dbsession, project_id, team_functions, **_FUNCTIONS_SCHEMA)
    await _post_rows(
        client,
        org_headers,
        personal_functions,
        [
            {
                "function_id": 0,
                "name": "send_digest",
                "implementation": "def send_digest():\n    return 'personal'",
                "argspec": "()",
                "language": "python",
            },
        ],
    )
    await _post_rows(
        client,
        org_headers,
        team_functions,
        [
            {
                "function_id": 0,
                "name": "send_digest",
                "implementation": "def send_digest():\n    return 'team'",
                "argspec": "()",
                "language": "python",
            },
        ],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.json()
    assert response.json()["detail"].startswith("team_memory_merge_function_conflict")


@pytest.mark.anyio
async def test_merge_dedupes_equivalent_recurring_tasks(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_task_dedup@test.com",
        org_name="Team Owned Merge Task Dedup Org",
    )
    personal_tasks = f"{personal_prefix}/Tasks"
    team_tasks = f"Teams/{team_id}/Tasks"
    _seed_table(dbsession, project_id, personal_tasks, **_LEGACY_PARENT_SCOPED_SCHEMA)
    _seed_table(dbsession, project_id, team_tasks, **_LEGACY_PARENT_SCOPED_SCHEMA)

    daily_digest = {
        "instance_id": 0,
        "name": "Daily digest",
        "description": "Send the daily digest email.",
        "status": "scheduled",
        "repeat": [{"frequency": "daily"}],
    }
    await _post_rows(
        client,
        org_headers,
        team_tasks,
        [{**daily_digest, "task_id": 0}],
    )
    await _post_rows(
        client,
        org_headers,
        personal_tasks,
        [
            {
                "task_id": 0,
                "instance_id": 0,
                "name": "Weekly report",
                "description": "Compile the weekly report.",
                "status": "scheduled",
                "repeat": [{"frequency": "weekly"}],
            },
            {**daily_digest, "task_id": 1},
        ],
    )
    # Machine-state rows referencing the personal task ids (no declared FK).
    personal_executions = f"{personal_prefix}/Tasks/Executions"
    _seed_table(dbsession, project_id, personal_executions)
    await _post_rows(
        client,
        org_headers,
        personal_executions,
        [
            {"task_id": 0, "marker": "weekly-activation"},
            {"task_id": 1, "marker": "daily-activation"},
        ],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    # The duplicate Daily digest was dropped (team's survives at task_id 0);
    # the unique Weekly report moved with task_id shifted 0 -> 1.
    team_rows = _table_rows(dbsession, project_id, team_tasks)
    by_name = {row["name"]: row["task_id"] for row in team_rows}
    assert by_name == {"Daily digest": 0, "Weekly report": 1}

    # Machine state followed both remap kinds: the dropped task's reference
    # maps to the surviving team task, the kept task's reference shifts.
    activation_rows = _table_rows(
        dbsession,
        project_id,
        f"Teams/{team_id}/Tasks/Executions",
    )
    by_marker = {row["marker"]: row["task_id"] for row in activation_rows}
    assert by_marker == {"weekly-activation": 1, "daily-activation": 0}


@pytest.mark.anyio
async def test_merge_reports_duplicate_contacts(
    client: AsyncClient,
    dbsession,
):
    owner_email = "team_owned_merge_dup_contacts@test.com"
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email=owner_email,
        org_name="Team Owned Merge Duplicate Contacts Org",
    )
    # The hire bootstrap seeded the personal boss row (contact_id 1) with the
    # owner's email; give the team book a row for the same person.
    team_contacts = f"Teams/{team_id}/Contacts"
    _seed_table(dbsession, project_id, team_contacts, **_CONTACTS_SCHEMA)
    await _post_rows(
        client,
        org_headers,
        team_contacts,
        [{"contact_id": 0, "first_name": "Boss", "email_address": owner_email}],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    body = response.json()["info"]

    # Both rows survive (contacts resolve by id, so duplicates are harmless
    # redundancy); the suspected match is reported in post-remap ids.
    assert body["duplicate_contacts"] == [
        {
            "matched_on": "email_address",
            "existing_contact_id": 0,
            "merged_contact_id": 2,
        },
    ]
    dbsession.expire_all()
    team_rows = _table_rows(dbsession, project_id, team_contacts)
    emails = [
        row["contact_id"]
        for row in team_rows
        if row.get("email_address") == owner_email
    ]
    assert sorted(emails) == [0, 2]


@pytest.mark.anyio
async def test_merge_dedupes_meta_singletons(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_meta@test.com",
        org_name="Team Owned Merge Meta Org",
    )
    # Sync-state singletons have a fixed meta_id=1 row and no auto-counted
    # column to remap; both sides populated would otherwise hard-collide.
    personal_meta = f"{personal_prefix}/Tasks/Meta"
    team_meta = f"Teams/{team_id}/Tasks/Meta"
    _seed_table(dbsession, project_id, personal_meta, unique_keys={"meta_id": "int"})
    _seed_table(dbsession, project_id, team_meta, unique_keys={"meta_id": "int"})
    await _post_rows(
        client,
        org_headers,
        personal_meta,
        [{"meta_id": 1, "custom_tasks_hash": "personal-hash"}],
    )
    await _post_rows(
        client,
        org_headers,
        team_meta,
        [{"meta_id": 1, "custom_tasks_hash": "team-hash"}],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    meta_rows = _table_rows(dbsession, project_id, team_meta)
    assert len(meta_rows) == 1
    assert meta_rows[0]["custom_tasks_hash"] == "team-hash"


@pytest.mark.anyio
async def test_merge_semantic_unique_keys_without_overlap(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_semantic_ok@test.com",
        org_name="Team Owned Merge Semantic Keys Org",
    )
    personal_data = f"{personal_prefix}/Data/Prospects"
    team_data = f"Teams/{team_id}/Data/Prospects"
    _seed_table(
        dbsession,
        project_id,
        personal_data,
        unique_keys={"github_login": "str"},
    )
    _seed_table(dbsession, project_id, team_data, unique_keys={"github_login": "str"})
    await _post_rows(
        client,
        org_headers,
        personal_data,
        [{"github_login": "hubot", "stars": 5}],
    )
    await _post_rows(
        client,
        org_headers,
        team_data,
        [{"github_login": "octocat", "stars": 9}],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    team_rows = _table_rows(dbsession, project_id, team_data)
    assert {row["github_login"] for row in team_rows} == {"hubot", "octocat"}


@pytest.mark.anyio
async def test_merge_rejects_semantic_unique_key_conflict(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_merge_semantic_conflict@test.com",
        org_name="Team Owned Merge Semantic Conflict Org",
    )
    personal_data = f"{personal_prefix}/Data/Prospects"
    team_data = f"Teams/{team_id}/Data/Prospects"
    _seed_table(
        dbsession,
        project_id,
        personal_data,
        unique_keys={"github_login": "str"},
    )
    _seed_table(dbsession, project_id, team_data, unique_keys={"github_login": "str"})
    await _post_rows(
        client,
        org_headers,
        personal_data,
        [{"github_login": "octocat", "stars": 5}],
    )
    await _post_rows(
        client,
        org_headers,
        team_data,
        [{"github_login": "octocat", "stars": 9}],
    )

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.json()
    assert response.json()["detail"].startswith("team_memory_merge_unique_key_conflict")
    assert "octocat" in response.json()["detail"]


def _head_snapshot_row_count(dbsession, project_id: int, context_name: str) -> int:
    """Rows captured by the context's HEAD commit snapshot."""
    context = (
        dbsession.query(Context)
        .filter_by(project_id=project_id, name=context_name)
        .one()
    )
    return dbsession.execute(
        text(
            """
            SELECT count(*)
            FROM log_event_version lev
            JOIN context_version cv ON cv.id = lev.context_version_id
            WHERE cv.context_id = :context_id
              AND cv.commit_hash = :commit_hash
            """,
        ),
        {"context_id": context.id, "commit_hash": context.current_commit_hash},
    ).scalar()


@pytest.mark.anyio
async def test_merge_rejects_versioned_collision_without_flag(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_versioned_refused@test.com",
        org_name="Team Owned Versioned Refused Org",
    )
    personal_data = f"{personal_prefix}/Data/Repos"
    team_data = f"Teams/{team_id}/Data/Repos"
    for name in (personal_data, team_data):
        _seed_table(dbsession, project_id, name, versioned=True)
    await _post_rows(client, org_headers, personal_data, [{"repo": "mine"}])
    await _post_rows(client, org_headers, team_data, [{"repo": "theirs"}])

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.json()
    assert response.json()["detail"].startswith("team_memory_merge_versioned_context")


@pytest.mark.anyio
async def test_merge_versioned_seals_head_with_merged_rows(
    client: AsyncClient,
    dbsession,
):
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_versioned_merge@test.com",
        org_name="Team Owned Versioned Merge Org",
    )
    personal_data = f"{personal_prefix}/Data/Repos"
    team_data = f"Teams/{team_id}/Data/Repos"
    for name in (personal_data, team_data):
        _seed_table(dbsession, project_id, name, versioned=True)
    await _post_rows(
        client,
        org_headers,
        personal_data,
        [{"repo": "mine-a"}, {"repo": "mine-b"}],
    )
    await _post_rows(client, org_headers, team_data, [{"repo": "theirs"}])

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={
            "owner_team_id": team_id,
            "merge_memory": True,
            "merge_versioned": True,
        },
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    body = response.json()["info"]
    assert body["versions_sealed"] >= 1

    dbsession.expire_all()
    rows = _table_rows(dbsession, project_id, team_data)
    assert {row["repo"] for row in rows} == {"mine-a", "mine-b", "theirs"}

    # The seal exists so HEAD explains the table's contents: without it a
    # rollback to HEAD would silently drop every merged row.
    assert _head_snapshot_row_count(dbsession, project_id, team_data) == len(rows)


@pytest.mark.anyio
async def test_versioned_rename_keeps_history_without_flag(
    client: AsyncClient,
    dbsession,
):
    """A versioned table with no counterpart moves by rename, history intact."""
    (
        _org_id,
        org_headers,
        team_id,
        agent_id,
        project_id,
        personal_prefix,
    ) = await _setup_merge_org(
        client,
        dbsession,
        email="team_owned_versioned_rename@test.com",
        org_name="Team Owned Versioned Rename Org",
    )
    personal_data = f"{personal_prefix}/Data/Repos"
    _seed_table(dbsession, project_id, personal_data, versioned=True)
    await _post_rows(client, org_headers, personal_data, [{"repo": "solo"}])
    context_dao = ContextDAO(dbsession)
    source = context_dao.filter(project_id=project_id, name=personal_data)[0][0]
    original_hash = context_dao.commit(source.id, commit_message="before transfer")

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id, "merge_memory": True},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    moved = (
        dbsession.query(Context)
        .filter_by(project_id=project_id, name=f"Teams/{team_id}/Data/Repos")
        .one()
    )
    assert moved.is_versioned is True
    assert moved.current_commit_hash == original_hash
