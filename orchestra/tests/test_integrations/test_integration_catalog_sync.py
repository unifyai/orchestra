"""Provider adapter invariants and unified integration catalog sync coverage."""

from __future__ import annotations

import importlib.util
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.core_models import Project
from orchestra.integrations.providers.base import ProviderExecutionRequest
from orchestra.integrations.providers.composio import ComposioProviderAdapter
from orchestra.integrations.providers.utils.pagination import ProviderPaginationError
from orchestra.services import builtins_integration_sync
from orchestra.services.builtins_integration_sync import (
    COMPOSITE_KEY_FIELD,
    BuiltinsSyncRequest,
    ensure_builtins_catalog_contexts,
    run_builtins_sync,
    upsert_context_rows,
    write_bootstrap_state,
)
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS
from orchestra.web.api.integrations import operations
from orchestra.workers import builtins_artifacts_seed_job


def _self_host_runner_module():
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "run_builtins_artifacts_seed_self_host.py"
    )
    spec = importlib.util.spec_from_file_location(
        "run_builtins_artifacts_seed_self_host",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            error = requests.HTTPError(
                f"{self.status_code} Client Error: Test Error",
            )
            error.response = self
            raise error
        return None


class FakeComposioCatalogAdapter:
    last_auth_config_was_created = False

    def list_toolkits(
        self,
        *,
        page_size: int = 1000,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "slug": "DISCORD",
                "name": "Discord",
                "description": "Discord servers and user data.",
                "category": "communication",
                "logo": "https://cdn.composio.dev/discord.svg",
                "auth_schemes": ["OAUTH2"],
                "version": "20260501_00",
            },
            {
                "slug": "GOOGLEDRIVE",
                "name": "Google Drive",
                "description": "Google Drive files.",
                "category": "files",
                "logo": "https://cdn.composio.dev/google-drive.svg",
                "auth_schemes": ["OAUTH2"],
                "version": "20260502_00",
            },
        ]

    def list_tools(
        self,
        *,
        toolkit_slug: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if toolkit_slug == "DISCORD":
            return [
                {
                    "slug": "DISCORD_LIST_MY_GUILDS",
                    "name": "List my guilds",
                    "description": "List Discord guilds.",
                    "toolkit": {"slug": "DISCORD"},
                    "input_parameters": {"type": "object"},
                    "output_parameters": {"type": "object"},
                    "scopes": ["guilds"],
                    "tags": ["readOnlyHint", "openWorldHint"],
                },
                {
                    "slug": "DISCORD_SEND_MESSAGE",
                    "name": "Send message",
                    "description": "Send a Discord channel message.",
                    "toolkit": {"slug": "DISCORD"},
                    "input_parameters": {"type": "object"},
                    "output_parameters": {"type": "object"},
                    "scopes": ["messages.write"],
                    "tags": ["createHint", "openWorldHint"],
                },
            ][:limit]
        return [
            {
                "slug": "GOOGLEDRIVE_SEARCH_FILES",
                "name": "Search files",
                "description": "Search Google Drive files.",
                "toolkit": {"slug": "GOOGLEDRIVE"},
                "input_parameters": {"type": "object"},
                "output_parameters": {"type": "object"},
                "scopes": ["drive.readonly"],
                "tags": ["readOnlyHint", "openWorldHint"],
            },
        ][:limit]

    def get_or_create_auth_config(self, toolkit_slug: str) -> str:
        return f"authcfg_{toolkit_slug.lower()}"


class FakeComposioCatalogAdapterWithAuthConfigFailure(FakeComposioCatalogAdapter):
    def list_toolkits(
        self,
        *,
        page_size: int = 1000,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "slug": "DISCORD",
                "name": "Discord",
                "auth_schemes": ["OAUTH2"],
            },
            {
                "slug": "BROKEN",
                "name": "Broken",
                "auth_schemes": ["OAUTH2"],
            },
        ]

    def list_tools(
        self,
        *,
        toolkit_slug: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if toolkit_slug == "BROKEN":
            return [
                {
                    "slug": "BROKEN_DO_THING",
                    "name": "Do thing",
                    "description": "Do a broken app thing.",
                    "toolkit": {"slug": "BROKEN"},
                    "input_parameters": {"type": "object"},
                    "output_parameters": {"type": "object"},
                    "scopes": [],
                    "tags": ["readOnlyHint"],
                },
            ][:limit]
        return super().list_tools(toolkit_slug=toolkit_slug, limit=limit)

    def get_or_create_auth_config(self, toolkit_slug: str) -> str:
        if toolkit_slug == "BROKEN":
            raise ValueError("Managed auth config rejected")
        return super().get_or_create_auth_config(toolkit_slug)


class FakePipedreamCatalogAdapter:
    def list_apps(self, *, has_components: bool | None = None) -> list[dict[str, Any]]:
        assert has_components is True
        return [
            {
                "id": "slack",
                "name_slug": "slack",
                "name": "Slack",
                "description": "Team messaging.",
                "categories": [{"name": "Communication"}],
                "img_src": "https://cdn.pipedream.com/slack.svg",
            },
            {
                "id": "github",
                "name_slug": "github",
                "name": "GitHub",
                "description": "Code hosting.",
            },
        ]

    def list_components(
        self,
        *,
        app: str,
        limit: int | None = None,
        component_type: str | None = None,
    ) -> list[dict[str, Any]]:
        assert component_type == "action"
        if app == "slack":
            return [
                {
                    "key": "slack-send-message",
                    "name": "Send Message",
                    "description": "Send a Slack message.",
                    "annotations": {
                        "destructiveHint": False,
                        "openWorldHint": True,
                        "readOnlyHint": False,
                    },
                    "props": {"channel": {"type": "string"}},
                },
            ][:limit]
        return [
            {
                "key": "github-list-repositories",
                "name": "List Repositories",
                "description": "List repositories.",
                "annotations": {
                    "destructiveHint": False,
                    "openWorldHint": True,
                    "readOnlyHint": True,
                },
            },
        ][:limit]


def _ensure_builtins_project(
    session: Session,
    *,
    user_id: str = "builtins-test",
) -> None:
    existing = (
        session.query(Project)
        .filter(Project.name == "Builtins", Project.user_id == user_id)
        .one_or_none()
    )
    if existing is None:
        session.add(
            Project(
                user_id=user_id,
                name="Builtins",
                is_public_read=True,
            ),
        )
    session.commit()


def test_builtins_context_upsert_updates_duplicate_function_id(
    dbsession: Session,
) -> None:
    _ensure_builtins_project(dbsession)
    contexts = ensure_builtins_catalog_contexts(dbsession)
    project = contexts["project"]
    tools_context = contexts["tools"]

    first = {
        "function_id": 400625911,
        "name": "primitives.integrations.gmail.old",
        "docstring": "old",
        "metadata": {"source": "provider_backed"},
    }
    second = {
        **first,
        "name": "primitives.integrations.gmail.new",
        "docstring": "new",
    }

    assert (
        upsert_context_rows(
            dbsession,
            project_id=project.id,
            context_id=tools_context.id,
            key_columns=["function_id"],
            rows=[first],
        )["inserted"]
        == 1
    )
    assert (
        upsert_context_rows(
            dbsession,
            project_id=project.id,
            context_id=tools_context.id,
            key_columns=["function_id"],
            rows=[second],
        )["updated"]
        == 1
    )
    dbsession.commit()

    rows = dbsession.execute(
        text(
            """
            SELECT le.data
            FROM log_event le
            JOIN log_event_context lec ON lec.log_event_id = le.id
            WHERE lec.context_id = :context_id
              AND le.data ->> 'function_id' = '400625911'
            """,
        ),
        {"context_id": tools_context.id},
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0]["docstring"] == "new"


def test_builtins_context_upsert_converges_under_parallel_same_key_writes(
    _engine,
) -> None:
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    with SessionLocal() as session:
        _ensure_builtins_project(session, user_id="builtins-parallel-test")
        contexts = ensure_builtins_catalog_contexts(session)
        project_id = contexts["project"].id
        tools_context_id = contexts["tools"].id

    def write_version(version: int) -> None:
        with SessionLocal() as session:
            upsert_context_rows(
                session,
                project_id=project_id,
                context_id=tools_context_id,
                key_columns=["function_id"],
                rows=[
                    {
                        "function_id": 400625912,
                        "name": "primitives.integrations.gmail.race",
                        "docstring": f"version-{version}",
                    },
                ],
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write_version, range(8)))

    with SessionLocal() as session:
        count = session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM log_event le
                JOIN log_event_context lec ON lec.log_event_id = le.id
                WHERE lec.context_id = :context_id
                  AND le.data ->> 'function_id' = '400625912'
                """,
            ),
            {"context_id": tools_context_id},
        ).scalar_one()
    assert count == 1


def test_builtins_context_upsert_handles_parallel_distinct_function_ids(
    _engine,
) -> None:
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    function_ids = list(range(400626000, 400626080))
    with SessionLocal() as session:
        _ensure_builtins_project(session, user_id="builtins-parallel-distinct-test")
        contexts = ensure_builtins_catalog_contexts(session)
        project_id = contexts["project"].id
        tools_context_id = contexts["tools"].id

    def write_batch(ids: list[int], generation: int) -> None:
        with SessionLocal() as session:
            upsert_context_rows(
                session,
                project_id=project_id,
                context_id=tools_context_id,
                key_columns=["function_id"],
                rows=[
                    {
                        "function_id": function_id,
                        "name": f"primitives.integrations.gmail.tool_{function_id}",
                        "docstring": f"generation-{generation}",
                    }
                    for function_id in ids
                ],
            )
            session.commit()

    batches = [
        function_ids[index : index + 10] for index in range(0, len(function_ids), 10)
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda ids: write_batch(ids, 1), batches))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda ids: write_batch(ids, 2), batches))

    with SessionLocal() as session:
        rows = session.execute(
            text(
                """
                SELECT le.data ->> 'function_id' AS function_id,
                       COUNT(*) AS row_count,
                       MAX(le.data ->> 'docstring') AS docstring
                FROM log_event le
                JOIN log_event_context lec ON lec.log_event_id = le.id
                WHERE lec.context_id = :context_id
                  AND le.data ->> 'function_id' = ANY(:function_ids)
                GROUP BY le.data ->> 'function_id'
                """,
            ),
            {
                "context_id": tools_context_id,
                "function_ids": [str(function_id) for function_id in function_ids],
            },
        ).fetchall()
        association_count = session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM log_event_context lec
                JOIN log_event le ON le.id = lec.log_event_id
                WHERE lec.context_id = :context_id
                  AND le.data ->> 'function_id' = ANY(:function_ids)
                """,
            ),
            {
                "context_id": tools_context_id,
                "function_ids": [str(function_id) for function_id in function_ids],
            },
        ).scalar_one()
        unique_count = session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM log_unique_constraint luc
                JOIN log_event le ON le.id = luc.log_event_id
                WHERE luc.context_id = :context_id
                  AND luc.field_name = :field_name
                  AND le.data ->> 'function_id' = ANY(:function_ids)
                """,
            ),
            {
                "context_id": tools_context_id,
                "field_name": COMPOSITE_KEY_FIELD,
                "function_ids": [str(function_id) for function_id in function_ids],
            },
        ).scalar_one()

    assert len(rows) == len(function_ids)
    assert {int(row.function_id) for row in rows} == set(function_ids)
    assert {row.row_count for row in rows} == {1}
    assert {row.docstring for row in rows} == {"generation-2"}
    assert association_count == len(function_ids)
    assert unique_count == len(function_ids)


def test_builtins_sync_skips_completed_tool_batch_before_provider_fetch(
    monkeypatch: pytest.MonkeyPatch,
    _engine,
) -> None:
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    with SessionLocal() as session:
        _ensure_builtins_project(session, user_id="builtins-checkpoint-test")

    fetch_calls: list[tuple[bool, tuple[str, ...]]] = []

    def fake_fetch(session: Session, body):
        fetch_calls.append((bool(body.sync_tools), tuple(body.app_slugs)))
        if not body.sync_tools:
            return builtins_integration_sync.ProviderCatalogFetchResult(
                apps=[
                    {
                        "backend_id": "composio",
                        "provider_app_id": "GMAIL",
                        "canonical_app_slug": "gmail",
                        "display_name": "Gmail",
                        "description": "Mail.",
                    },
                ],
                tools=[],
                skipped_apps=[],
                requested_app_slugs=[],
                matched_app_slugs=["gmail"],
                sync_mode="full",
                cache_version="cache-v1",
            )
        return builtins_integration_sync.ProviderCatalogFetchResult(
            apps=[],
            tools=[
                {
                    "backend_id": "composio",
                    "provider_app_id": "GMAIL",
                    "canonical_app_slug": "gmail",
                    "provider_tool_id": "GMAIL_FETCH",
                    "name": "fetch",
                    "display_name": "Fetch",
                    "description": "Fetch mail.",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                },
            ],
            skipped_apps=[],
            requested_app_slugs=list(body.app_slugs),
            matched_app_slugs=["gmail"],
            sync_mode="partial",
            cache_version="cache-v1",
        )

    monkeypatch.setattr(builtins_integration_sync, "fetch_provider_catalog", fake_fetch)

    request = BuiltinsSyncRequest(
        backend_id="composio",
        environment="test",
        desired_hash="desired-1",
        cache_version="cache-v1",
        sync_payload={
            "backend_id": "composio",
            "sync_mode": "full",
            "include_all_managed_apps": True,
            "sync_tools": True,
        },
        batch_size=1,
        workers=1,
    )

    first = run_builtins_sync(SessionLocal, request)
    second = run_builtins_sync(SessionLocal, request)

    assert first.completed_batches == 1
    assert second.skipped_batches == 1
    assert fetch_calls == [
        (False, ()),
        (True, ("GMAIL",)),
        (False, ()),
    ]


def test_builtins_sync_prunes_stale_builtins_app_and_tool_rows(
    monkeypatch: pytest.MonkeyPatch,
    dbsession: Session,
    _engine,
) -> None:
    _ensure_builtins_project(dbsession, user_id="builtins-prune-test")
    contexts = ensure_builtins_catalog_contexts(dbsession)
    project = contexts["project"]
    upsert_context_rows(
        dbsession,
        project_id=project.id,
        context_id=contexts["apps"].id,
        key_columns=["app_id"],
        rows=[
            {
                "app_id": 991001,
                "backend_id": "composio",
                "canonical_app_slug": "stale_app",
                "display_name": "Stale App",
            },
        ],
    )
    upsert_context_rows(
        dbsession,
        project_id=project.id,
        context_id=contexts["tools"].id,
        key_columns=["function_id"],
        rows=[
            {
                "function_id": 991002,
                "backend_id": "composio",
                "name": "primitives.integrations.gmail.stale",
                "metadata": {
                    "integration": {
                        "app_slug": "gmail",
                    },
                },
            },
        ],
    )
    dbsession.commit()
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

    def fake_fetch(session: Session, body):
        if not body.sync_tools:
            return builtins_integration_sync.ProviderCatalogFetchResult(
                apps=[
                    {
                        "backend_id": "composio",
                        "provider_app_id": "GMAIL",
                        "canonical_app_slug": "gmail",
                        "display_name": "Gmail",
                        "description": "Mail.",
                    },
                ],
                tools=[],
                skipped_apps=[],
                requested_app_slugs=[],
                matched_app_slugs=["gmail"],
                sync_mode="full",
                cache_version="cache-prune-v1",
            )
        return builtins_integration_sync.ProviderCatalogFetchResult(
            apps=[],
            tools=[
                {
                    "backend_id": "composio",
                    "provider_app_id": "GMAIL",
                    "canonical_app_slug": "gmail",
                    "provider_tool_id": "GMAIL_FETCH",
                    "name": "fetch",
                    "display_name": "Fetch",
                    "description": "Fetch mail.",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                },
            ],
            skipped_apps=[],
            requested_app_slugs=list(body.app_slugs),
            matched_app_slugs=["gmail"],
            sync_mode="partial",
            cache_version="cache-prune-v1",
        )

    monkeypatch.setattr(builtins_integration_sync, "fetch_provider_catalog", fake_fetch)

    result = run_builtins_sync(
        SessionLocal,
        BuiltinsSyncRequest(
            backend_id="composio",
            environment="test",
            desired_hash="desired-prune-1",
            cache_version="cache-prune-v1",
            prune_unlisted_apps=True,
            sync_payload={
                "backend_id": "composio",
                "sync_mode": "full",
                "include_all_managed_apps": True,
                "sync_tools": True,
            },
            batch_size=1,
            workers=1,
        ),
    )

    assert result.apps_pruned == 1
    assert result.tools_pruned == 1
    remaining = dbsession.execute(
        text(
            """
            SELECT COUNT(*)
            FROM log_event le
            JOIN log_event_context lec ON lec.log_event_id = le.id
            WHERE lec.context_id IN (:apps_context_id, :tools_context_id)
              AND (
                  le.data ->> 'app_id' = '991001'
                  OR le.data ->> 'function_id' = '991002'
              )
            """,
        ),
        {
            "apps_context_id": contexts["apps"].id,
            "tools_context_id": contexts["tools"].id,
        },
    ).scalar_one()
    assert remaining == 0


def test_builtins_catalog_queries_are_scoped_to_the_builtins_partition(
    dbsession: Session,
) -> None:
    """Catalog reads and prunes must never touch another project's partition.

    ``log_event.id`` is unique only within a partition (the PK is
    ``(project_id, id, owner_key)``), so two projects can hold rows with the same
    ``id``. The catalog lookup/prune filter by ``context_id`` and JSONB fields;
    without a ``project_id`` predicate Postgres scans every partition and the
    ``lec.log_event_id = le.id`` join can cross-match a different project's row
    that happens to share the id. This seeds such a decoy and asserts the
    Builtins-scoped queries neither read nor delete it.
    """

    _ensure_builtins_project(dbsession, user_id="builtins-scope-test")
    contexts = ensure_builtins_catalog_contexts(dbsession)
    project = contexts["project"]
    tools_context = contexts["tools"]

    upsert_context_rows(
        dbsession,
        project_id=project.id,
        context_id=tools_context.id,
        key_columns=["function_id"],
        rows=[
            {
                "function_id": 994001,
                "backend_id": "composio",
                "name": "primitives.integrations.gmail.real",
                "metadata": {"integration": {"app_slug": "gmail"}},
            },
        ],
    )
    dbsession.commit()

    builtins_log_id = dbsession.execute(
        text(
            """
            SELECT le.id
            FROM log_event le
            JOIN log_event_context lec ON lec.log_event_id = le.id
            WHERE lec.context_id = :context_id
              AND le.data ->> 'function_id' = '994001'
            """,
        ),
        {"context_id": tools_context.id},
    ).scalar_one()

    # Decoy: a different project reusing the same log_event id, with a context
    # row pointing at the Builtins context. An unscoped join would reach it.
    decoy = Project(user_id="decoy-scope-test", name="Decoy Scope")
    dbsession.add(decoy)
    dbsession.flush()
    dbsession.execute(
        text(
            """
            INSERT INTO log_event (id, project_id, data, owner_key)
            VALUES (:id, :project_id, CAST(:data AS jsonb), 'sys')
            """,
        ),
        {
            "id": builtins_log_id,
            "project_id": decoy.id,
            "data": json.dumps(
                {
                    "function_id": 994999,
                    "backend_id": "composio",
                    "metadata": {"integration": {"app_slug": "decoyapp"}},
                },
            ),
        },
    )
    dbsession.execute(
        text(
            """
            INSERT INTO log_event_context (project_id, log_event_id, context_id, owner_key)
            VALUES (:project_id, :log_event_id, :context_id, 'sys')
            """,
        ),
        {
            "project_id": decoy.id,
            "log_event_id": builtins_log_id,
            "context_id": tools_context.id,
        },
    )
    dbsession.commit()

    # Read path: the decoy's function_id only exists in the decoy project, so a
    # Builtins-scoped lookup must miss it (an unscoped join would return the id).
    assert (
        builtins_integration_sync._find_existing_log_id(
            dbsession,
            project_id=project.id,
            context_id=tools_context.id,
            key_columns=["function_id"],
            key_values={"function_id": "994999"},
            key_hash="unused-no-constraint-row",
        )
        is None
    )
    # The genuine Builtins row is still resolved within its own partition.
    assert (
        builtins_integration_sync._find_existing_log_id(
            dbsession,
            project_id=project.id,
            context_id=tools_context.id,
            key_columns=["function_id"],
            key_values={"function_id": "994001"},
            key_hash="unused-no-constraint-row",
        )
        == builtins_log_id
    )

    # Delete path: pruning the Builtins catalog (decoy slug is "unlisted") must
    # not delete the decoy's log_event in the other project.
    builtins_integration_sync.prune_tool_rows_for_unlisted_apps(
        dbsession,
        project_id=project.id,
        context_id=tools_context.id,
        backend_id="composio",
        keep_app_slugs=["gmail"],
    )
    dbsession.commit()

    decoy_survives = dbsession.execute(
        text(
            """
            SELECT COUNT(*)
            FROM log_event
            WHERE project_id = :project_id AND id = :id
            """,
        ),
        {"project_id": decoy.id, "id": builtins_log_id},
    ).scalar_one()
    assert decoy_survives == 1


def _seed_orphan_tool_row(dbsession: Session, contexts, *, function_id: int) -> None:
    upsert_context_rows(
        dbsession,
        project_id=contexts["project"].id,
        context_id=contexts["tools"].id,
        key_columns=["function_id"],
        rows=[
            {
                "function_id": function_id,
                "backend_id": "composio",
                "name": "primitives.integrations.listennotes.search",
                "metadata": {"integration": {"app_slug": "listennotes"}},
            },
        ],
    )
    dbsession.commit()


def _listen_notes_fetch(session: Session, body):
    if not body.sync_tools:
        return builtins_integration_sync.ProviderCatalogFetchResult(
            apps=[
                {
                    "backend_id": "composio",
                    "provider_app_id": "LISTENNOTES",
                    "canonical_app_slug": "listen_notes",
                    "display_name": "Listen Notes",
                    "description": "Podcasts.",
                },
            ],
            tools=[],
            skipped_apps=[],
            requested_app_slugs=list(body.app_slugs),
            matched_app_slugs=["listen_notes"],
            sync_mode="full" if not body.app_slugs else "partial",
            cache_version="cache-orphan-v1",
        )
    return builtins_integration_sync.ProviderCatalogFetchResult(
        apps=[],
        tools=[
            {
                "backend_id": "composio",
                "provider_app_id": "LISTENNOTES",
                "canonical_app_slug": "listen_notes",
                "provider_tool_id": "LISTENNOTES_SEARCH",
                "name": "search",
                "display_name": "Search",
                "description": "Search podcasts.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        ],
        skipped_apps=[],
        requested_app_slugs=list(body.app_slugs),
        matched_app_slugs=["listen_notes"],
        sync_mode="partial",
        cache_version="cache-orphan-v1",
    )


def _count_tool_rows(dbsession: Session, contexts, *, where: str, params: dict) -> int:
    return dbsession.execute(
        text(
            f"""
            SELECT COUNT(*)
            FROM log_event le
            JOIN log_event_context lec ON lec.log_event_id = le.id
            WHERE lec.context_id = :tools_context_id
              AND {where}
            """,
        ),
        {"tools_context_id": contexts["tools"].id, **params},
    ).scalar_one()


def test_builtins_full_sync_prunes_renamed_or_removed_app_tools(
    monkeypatch: pytest.MonkeyPatch,
    dbsession: Session,
    _engine,
) -> None:
    """A full sync removes tool rows whose app slug is no longer in the catalog.

    The old ``listennotes`` rows live under a slug no batch revisits (the app is
    now ``listen_notes``), so only the finalization prune can reach them.
    """

    _ensure_builtins_project(dbsession, user_id="builtins-orphan-full")
    contexts = ensure_builtins_catalog_contexts(dbsession)
    _seed_orphan_tool_row(dbsession, contexts, function_id=992001)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

    monkeypatch.setattr(
        builtins_integration_sync,
        "fetch_provider_catalog",
        _listen_notes_fetch,
    )

    result = run_builtins_sync(
        SessionLocal,
        BuiltinsSyncRequest(
            backend_id="composio",
            environment="test",
            desired_hash="desired-orphan-1",
            cache_version="cache-orphan-v1",
            prune_unlisted_apps=True,
            sync_payload={
                "backend_id": "composio",
                "sync_mode": "full",
                "include_all_managed_apps": True,
                "sync_tools": True,
            },
            batch_size=1,
            workers=1,
        ),
    )

    assert result.tools_pruned >= 1
    assert (
        _count_tool_rows(
            dbsession,
            contexts,
            where="le.data ->> 'function_id' = '992001'",
            params={},
        )
        == 0
    )
    assert (
        _count_tool_rows(
            dbsession,
            contexts,
            where="(le.data #>> '{metadata,integration,app_slug}') = :slug",
            params={"slug": "listen_notes"},
        )
        >= 1
    )


def test_builtins_partial_sync_keeps_unlisted_app_tools(
    monkeypatch: pytest.MonkeyPatch,
    dbsession: Session,
    _engine,
) -> None:
    """A partial sync must not run the finalization prune (it would delete tools
    for every app outside the requested subset)."""

    _ensure_builtins_project(dbsession, user_id="builtins-orphan-partial")
    contexts = ensure_builtins_catalog_contexts(dbsession)
    _seed_orphan_tool_row(dbsession, contexts, function_id=993001)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

    monkeypatch.setattr(
        builtins_integration_sync,
        "fetch_provider_catalog",
        _listen_notes_fetch,
    )

    run_builtins_sync(
        SessionLocal,
        BuiltinsSyncRequest(
            backend_id="composio",
            environment="test",
            desired_hash="desired-orphan-2",
            cache_version="cache-orphan-v1",
            mode="partial",
            app_slugs=["listen_notes"],
            prune_unlisted_apps=True,
            sync_payload={
                "backend_id": "composio",
                "sync_mode": "partial",
                "app_slugs": ["listen_notes"],
                "sync_tools": True,
            },
            batch_size=1,
            workers=1,
        ),
    )

    assert (
        _count_tool_rows(
            dbsession,
            contexts,
            where="le.data ->> 'function_id' = '993001'",
            params={},
        )
        == 1
    )


def test_builtins_worker_loads_request_file_without_gcp(tmp_path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "backend_id": "composio",
                "environment": "selfhost",
                "desired_hash": "desired-worker-file",
                "cache_version": "cache-worker-file",
            },
        ),
        encoding="utf-8",
    )

    args = builtins_artifacts_seed_job._parse_args(
        ["--request-file", str(request_file)],
    )
    payload = builtins_artifacts_seed_job._load_request_payload(args)

    assert payload["backend_id"] == "composio"
    assert payload["desired_hash"] == "desired-worker-file"


def test_builtins_worker_bootstrap_state_writer_records_failure(
    dbsession: Session,
) -> None:
    request = BuiltinsSyncRequest(
        backend_id="composio",
        environment="selfhost",
        desired_hash="desired-failed-run",
        cache_version="cache-failed-run",
        desired_config={"sync": {"mode": "partial"}},
        run_id="run-failed-1",
    )

    write_bootstrap_state(
        dbsession,
        request=request,
        status="failed",
        error="forced failure",
    )
    dbsession.commit()

    row = dbsession.execute(
        text(
            """
            SELECT desired_hash, last_status, last_error, last_sync_diagnostics_json
            FROM integration_bootstrap_state
            WHERE environment = 'selfhost' AND backend_id = 'composio'
            """,
        ),
    ).one()
    assert row.desired_hash == "desired-failed-run"
    assert row.last_status == "failed"
    assert row.last_error == "forced failure"
    assert row.last_sync_diagnostics_json["run_id"] == "run-failed-1"


def test_self_host_runner_builds_direct_worker_request_without_gcp_dependency() -> None:
    module = _self_host_runner_module()
    source = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "run_builtins_artifacts_seed_self_host.py"
    ).read_text(encoding="utf-8")
    assert "google.cloud" not in source
    assert "gcloud" not in source
    assert "gs://" not in source

    manifest = {
        "schema_version": 1,
        "environment": "selfhost",
        "providers": {
            "composio": {
                "status": "enabled",
                "sync": {
                    "mode": "partial",
                    "app_slugs": ["gmail"],
                    "prune_unlisted_apps": True,
                },
            },
        },
    }

    request = module.build_request_from_manifest(
        manifest,
        backend_id="composio",
        workers=2,
        batch_size=7,
    )

    assert request["environment"] == "selfhost"
    assert request["desired_hash"]
    assert request["cache_version"].startswith("public-builtins-selfhost-composio-")
    assert request["sync_payload"]["app_slugs"] == ["gmail"]
    assert request["prune_unlisted_apps"] is True
    assert request["workers"] == 2
    assert request["batch_size"] == 7


def test_composio_full_sync_handler_does_not_eagerly_create_auth_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        config_json = {}
        status = "enabled"

    class FakeDAO:
        def __init__(self, session) -> None:
            self.session = session

        def get_backend(self, backend_id: str):
            assert backend_id == "composio"
            return FakeBackend()

    class NoEagerAuthConfigAdapter(FakeComposioCatalogAdapterWithAuthConfigFailure):
        def get_or_create_auth_config(self, toolkit_slug: str) -> str:
            raise AssertionError("full sync should not create Composio auth configs")

    def fake_sync_catalog_rows(session, body):
        assert sorted(app["canonical_app_slug"] for app in body.apps) == [
            "broken",
            "discord",
        ]
        assert len(body.tools) == 2
        return {"apps_upserted": len(body.apps), "tools_upserted": len(body.tools)}

    monkeypatch.setattr(
        operations,
        "seed_default_provider_catalog",
        lambda session: None,
    )
    monkeypatch.setattr(operations, "IntegrationProviderDAO", FakeDAO)
    monkeypatch.setattr(
        operations,
        "get_provider_adapter",
        lambda *_args, **_kwargs: NoEagerAuthConfigAdapter(),
    )
    monkeypatch.setattr(operations, "_sync_catalog_rows", fake_sync_catalog_rows)

    response = operations._composio_live_catalog_handler(
        session=object(),
        body=operations.IntegrationCatalogSyncRequest(
            backend_id="composio",
            sync_mode="full",
            include_all_managed_apps=True,
            create_auth_configs=True,
            tool_limit_per_app=1,
        ),
    )

    assert response.status == "success"
    assert response.apps_upserted == 2
    assert response.tools_upserted == 2
    assert sorted(response.matched_app_slugs) == ["broken", "discord"]
    assert response.skipped_apps == []


def test_composio_app_only_sync_does_not_fetch_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        config_json = {}
        status = "enabled"

    class FakeDAO:
        def __init__(self, session) -> None:
            self.session = session

        def get_backend(self, backend_id: str):
            assert backend_id == "composio"
            return FakeBackend()

    class AppOnlyAdapter(FakeComposioCatalogAdapter):
        def list_tools(self, *, toolkit_slug: str, limit: int | None = None):
            raise AssertionError("app-only sync must not fetch toolkit tools")

    def fake_sync_catalog_rows(session, body):
        assert len(body.apps) == 2
        assert body.tools == []
        return {"apps_upserted": 2, "tools_upserted": 0}

    monkeypatch.setattr(
        operations,
        "seed_default_provider_catalog",
        lambda session: None,
    )
    monkeypatch.setattr(operations, "IntegrationProviderDAO", FakeDAO)
    monkeypatch.setattr(
        operations,
        "get_provider_adapter",
        lambda *_args, **_kwargs: AppOnlyAdapter(),
    )
    monkeypatch.setattr(operations, "_sync_catalog_rows", fake_sync_catalog_rows)

    response = operations._composio_live_catalog_handler(
        session=object(),
        body=operations.IntegrationCatalogSyncRequest(
            backend_id="composio",
            sync_mode="full",
            include_all_managed_apps=True,
            sync_tools=False,
        ),
    )

    assert response.status == "success"
    assert response.apps_upserted == 2
    assert response.tools_upserted == 0


def test_composio_connect_lazily_creates_missing_auth_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        config_json = {}
        status = "enabled"

    class FakeApp:
        raw_provider_metadata_json = {}

    class FakeConnection:
        backend_id = "composio"
        provider_app_id = "DISCORD"
        connection_id = "ic_test"
        provider_connection_id = None

    class FakeAdapter:
        def __init__(self) -> None:
            self.created_for: list[str] = []

        def get_or_create_auth_config(self, toolkit_slug: str) -> str:
            self.created_for.append(toolkit_slug)
            return "authcfg_discord"

        def create_auth_link(
            self,
            *,
            user_id,
            auth_config_id,
            callback_url=None,
            alias=None,
        ):
            assert auth_config_id == "authcfg_discord"
            assert alias == "ic_test"
            return "https://backend.composio.dev/connect/discord", "ca_discord", None

    adapter = FakeAdapter()
    app = FakeApp()
    conn = FakeConnection()

    monkeypatch.setattr(
        operations,
        "get_provider_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    url = operations._provider_connect_url(
        backend=FakeBackend(),
        app=app,
        owner=operations.OwnerContext(user_id="user-1"),
        connection=conn,
        redirect_url="https://console.example/callback",
    )

    assert url == "https://backend.composio.dev/connect/discord"
    assert adapter.created_for == ["DISCORD"]
    assert app.raw_provider_metadata_json["auth_config_id"] == "authcfg_discord"
    assert conn.provider_connection_id == "ca_discord"


def test_composio_connect_logs_auth_config_creation_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeBackend:
        config_json = {}
        status = "enabled"

    class FakeApp:
        canonical_app_slug = "discord"
        raw_provider_metadata_json = {}

    class FakeConnection:
        backend_id = "composio"
        provider_app_id = "DISCORD"
        canonical_app_slug = "discord"
        connection_id = "ic_failure"

    class FakeProviderResponse:
        status_code = 400
        text = '{"message":"invalid toolkit auth config"}'

    class FakeAdapter:
        def get_or_create_auth_config(self, toolkit_slug: str) -> str:
            exc = RuntimeError(f"provider rejected {toolkit_slug}")
            exc.response = FakeProviderResponse()
            raise exc

        def create_auth_link(self, **kwargs):
            raise AssertionError("auth link should not be created after config failure")

    monkeypatch.setattr(
        operations,
        "get_provider_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    caplog.set_level(logging.ERROR, logger=operations.__name__)

    with pytest.raises(operations.ProviderConnectError) as excinfo:
        operations._provider_connect_url(
            backend=FakeBackend(),
            app=FakeApp(),
            owner=operations.OwnerContext(owner_scope="assistant", user_id="user-1"),
            connection=FakeConnection(),
            redirect_url="https://console.example/callback",
        )

    assert excinfo.value.code == "provider_auth_config_failed"
    assert "Could not start the" in str(excinfo.value)

    assert "Composio connect failure stage=auth_config_create" in caplog.text
    assert "backend_id=composio" in caplog.text
    assert "provider_app_id=DISCORD" in caplog.text
    assert "canonical_app_slug=discord" in caplog.text
    assert "connection_id=ic_failure" in caplog.text
    assert "provider_status_code=400" in caplog.text
    assert "invalid toolkit auth config" in caplog.text


def test_composio_adapter_fetches_catalog_and_manages_auth_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_get(url, *, headers, params=None, timeout):
        calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
            },
        )
        if url.endswith("/toolkits"):
            return FakeResponse({"items": [{"slug": "DISCORD", "name": "Discord"}]})
        if url.endswith("/tools"):
            return FakeResponse(
                {
                    "items": [
                        {
                            "slug": "DISCORD_LIST_MY_GUILDS",
                            "toolkit": {"slug": "DISCORD"},
                        },
                    ],
                },
            )
        if url.endswith("/auth_configs"):
            return FakeResponse({"items": []})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, *, headers, json, timeout):
        calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            },
        )
        if url.endswith("/auth_configs"):
            return FakeResponse({"id": "authcfg_discord"})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)
    adapter = ComposioProviderAdapter(api_key="composio-key", timeout_seconds=9)

    assert adapter.list_toolkits() == [{"slug": "DISCORD", "name": "Discord"}]
    assert adapter.list_tools(toolkit_slug="DISCORD") == [
        {"slug": "DISCORD_LIST_MY_GUILDS", "toolkit": {"slug": "DISCORD"}},
    ]
    assert adapter.get_or_create_auth_config("DISCORD") == "authcfg_discord"
    assert {call["method"] for call in calls} == {"GET", "POST"}


def test_composio_canonical_slug_request_still_fetches_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requests keyed by the derived canonical slug must resolve to the real
    provider toolkit slug.

    The display name "Listen Notes" derives the canonical slug ``listen_notes``,
    which differs from the provider's real slug ``LISTENNOTES``. The tool phase
    batches by canonical slug, so without alias resolution the toolkit is dropped
    as ``not_found`` and the app silently materializes zero tools.
    """

    def fake_get(url, *, headers, params=None, timeout):
        if url.endswith("/toolkits"):
            return FakeResponse(
                {"items": [{"slug": "LISTENNOTES", "name": "Listen Notes"}]},
            )
        if url.endswith("/tools"):
            assert (params or {}).get("toolkit_slug") == "LISTENNOTES"
            return FakeResponse(
                {
                    "items": [
                        {
                            "slug": "LISTENNOTES_SEARCH",
                            "toolkit": {"slug": "LISTENNOTES"},
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr("requests.get", fake_get)
    adapter = ComposioProviderAdapter(api_key="composio-key", timeout_seconds=9)

    app_entries = adapter.list_app_entries(
        app_slugs=["listen_notes"],
        include_detail=False,
    )
    assert [entry["canonical_app_slug"] for entry in app_entries] == ["listen_notes"]
    assert [entry["provider_app_id"] for entry in app_entries] == ["LISTENNOTES"]
    assert adapter.last_skipped_apps == []

    tool_entries = adapter.list_tool_entries(
        app_slug=app_entries[0]["canonical_app_slug"],
        provider_app_id=app_entries[0]["provider_app_id"],
    )
    assert [tool["canonical_app_slug"] for tool in tool_entries] == ["listen_notes"]
    assert tool_entries[0]["provider_app_id"] == "LISTENNOTES"
    assert tool_entries[0]["name"] == "search"


def test_composio_adapter_uses_bounded_cursor_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor_calls: list[str | None] = []

    def fake_get(url, *, headers, params=None, timeout):
        assert url.endswith("/toolkits")
        cursor_calls.append((params or {}).get("cursor"))
        if not (params or {}).get("cursor"):
            return FakeResponse(
                {"items": [{"slug": "A"}], "next_cursor": "cursor-2", "total_items": 2},
            )
        return FakeResponse(
            {"items": [{"slug": "B"}], "next_cursor": None, "total_items": 2},
        )

    monkeypatch.setattr("requests.get", fake_get)
    adapter = ComposioProviderAdapter(
        api_key="composio-key",
        timeout_seconds=9,
        max_pages=5,
    )

    assert adapter.list_toolkits(page_size=1) == [{"slug": "A"}, {"slug": "B"}]
    assert cursor_calls == [None, "cursor-2"]


def test_composio_adapter_rejects_repeated_pagination_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, *, headers, params=None, timeout):
        return FakeResponse({"items": [{"slug": "A"}], "next_cursor": "same-cursor"})

    monkeypatch.setattr("requests.get", fake_get)
    adapter = ComposioProviderAdapter(
        api_key="composio-key",
        timeout_seconds=9,
        max_pages=5,
    )

    with pytest.raises(ProviderPaginationError):
        adapter.list_toolkits(page_size=1)


def test_provider_action_class_uses_native_hints_not_descriptions() -> None:
    assert operations._composio_behavior_hints(
        {
            "slug": "GMAIL_LIST_LABELS",
            "name": "List Gmail labels",
            "description": "Labels can be added or removed elsewhere.",
            "tags": ["readOnlyHint", "openWorldHint"],
        },
    ) == ["read_only", "external"]
    assert (
        operations._composio_action_class(
            {
                "slug": "GMAIL_LIST_LABELS",
                "name": "List Gmail labels",
                "description": "Labels can be added or removed elsewhere.",
                "tags": ["readOnlyHint", "openWorldHint"],
            },
        )
        == "read"
    )
    assert operations._composio_behavior_hints(
        {
            "slug": "GMAIL_DELETE_DRAFT",
            "name": "Delete draft",
            "tags": ["destructiveHint", "idempotentHint", "openWorldHint"],
        },
    ) == ["mutates_state", "destructive", "idempotent", "external"]
    assert (
        operations._composio_action_class(
            {
                "slug": "GMAIL_DELETE_DRAFT",
                "name": "Delete draft",
                "tags": ["destructiveHint", "idempotentHint", "openWorldHint"],
            },
        )
        == "destructive"
    )
    assert operations._composio_behavior_hints(
        {
            "slug": "GMAIL_SEND_EMAIL",
            "name": "Send email",
            "tags": ["createHint", "openWorldHint"],
        },
    ) == ["mutates_state", "external", "creates_resource"]
    assert (
        operations._composio_action_class(
            {
                "slug": "GMAIL_SEND_EMAIL",
                "name": "Send email",
                "tags": ["createHint", "openWorldHint"],
            },
        )
        == "write"
    )
    assert operations._pipedream_behavior_hints(
        {
            "key": "stripe-search-customers",
            "annotations": {
                "destructiveHint": False,
                "openWorldHint": True,
                "readOnlyHint": True,
            },
        },
    ) == ["read_only", "external"]
    assert (
        operations._pipedream_action_class(
            {
                "key": "stripe-search-customers",
                "annotations": {
                    "destructiveHint": False,
                    "openWorldHint": True,
                    "readOnlyHint": True,
                },
            },
        )
        == "read"
    )


def test_composio_execute_preserves_provider_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url, *, headers, json, timeout):
        assert url.endswith("/tools/execute/GMAIL_LIST_LABELS")
        assert json["user_id"] == "assistant:2115"
        assert json["connected_account_id"] == "ca_test"
        return FakeResponse(
            {
                "error": {
                    "message": "Connected account user ID does not match.",
                    "slug": "ActionExecute_ConnectedAccountEntityIdMismatch",
                },
            },
            status_code=400,
        )

    monkeypatch.setattr("requests.post", fake_post)
    adapter = ComposioProviderAdapter(
        api_key="composio-key",
        timeout_seconds=9,
    )

    result = adapter.execute(
        ProviderExecutionRequest(
            backend_id="composio",
            tool_id="composio:gmail:list_labels",
            canonical_app_slug="gmail",
            provider_tool_id="GMAIL_LIST_LABELS",
            connection_id="ic_test",
            provider_connection_id="ca_test",
            action_class="read",
            user_id="assistant:2115",
            arguments={"user_id": "me"},
        ),
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error["code"] == "provider_request_failed"
    assert result.error["provider_status_code"] == 400
    assert (
        "ActionExecute_ConnectedAccountEntityIdMismatch"
        in result.error["provider_response_body"]
    )
    assert result.error["provider_request"] == {
        "provider_tool_id": "GMAIL_LIST_LABELS",
        "payload_keys": ["arguments", "connected_account_id", "user_id"],
        "argument_keys": ["user_id"],
        "user_id_present": True,
        "connected_account_id_present": True,
    }


@pytest.mark.anyio
async def test_live_composio_oauth_connect_route_uses_backend_config(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnectAdapter:
        def get_or_create_auth_config(self, toolkit_slug: str) -> str:
            assert toolkit_slug == "DISCORD"
            return "authcfg_discord"

        def create_auth_link(
            self,
            *,
            user_id,
            auth_config_id,
            callback_url=None,
            alias=None,
        ):
            assert user_id == "integration-user"
            assert auth_config_id == "authcfg_discord"
            assert alias and alias.startswith("ic_")
            assert (
                callback_url
                == f"http://localhost:3000/integrations/callback?connection_id={alias}"
            )
            return "https://backend.composio.dev/connect/discord", "ca_discord", None

    monkeypatch.setattr(
        "orchestra.web.api.integrations.operations.get_provider_adapter",
        lambda *_args, **_kwargs: FakeConnectAdapter(),
    )

    backend = await client.patch(
        "/v0/admin/integrations/backends/composio",
        headers=ADMIN_HEADERS,
        json={"status": "enabled"},
    )
    assert backend.status_code == status.HTTP_200_OK, backend.json()

    start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            "owner_scope": "assistant",
            "assistant_id": 123,
            "user_id": "integration-user",
            "canonical_app_slug": "discord",
            "backend_id": "composio",
            "provider_app_id": "DISCORD",
            "requested_scopes": ["guilds"],
            "auth_mode": "oauth",
            "redirect_url": "http://localhost:3000/integrations/callback",
        },
    )
    assert start.status_code == status.HTTP_200_OK, start.json()
    assert start.json()["connect_url"] == "https://backend.composio.dev/connect/discord"
    assert start.json()["connection"]["provider_connection_id"] == "ca_discord"


@pytest.mark.anyio
async def test_builtins_sync_start_launches_job_and_returns_running(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestra.services import builtins_seed_launcher

    monkeypatch.setattr(
        builtins_seed_launcher,
        "builtins_seed_job_configured",
        lambda: True,
    )

    uploaded: dict[str, Any] = {}

    def fake_upload(payload: dict[str, Any]) -> str:
        uploaded["payload"] = payload
        return f"gs://bucket/builtins-seed-requests/{payload['run_id']}.json"

    executed: list[str] = []
    monkeypatch.setattr(builtins_seed_launcher, "upload_seed_request", fake_upload)
    monkeypatch.setattr(
        builtins_seed_launcher,
        "execute_seed_job",
        lambda request_uri: executed.append(request_uri),
    )

    response = await client.post(
        "/v0/admin/integrations/builtins-sync/start",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "composio",
            "environment": "test-async",
            "desired_hash": "hash-async-1",
            "cache_version": "cache-async-1",
            "mode": "all",
            "sync_payload": {"backend_id": "composio", "sync_mode": "full"},
        },
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    body = response.json()
    assert body["status"] == "running"
    run_id = body["run_id"]
    assert run_id
    request_uri = body["request_uri"]
    assert request_uri.endswith(f"{run_id}.json")
    assert executed == [request_uri]
    assert uploaded["payload"]["run_id"] == run_id

    state = await client.get(
        "/v0/admin/integrations/bootstrap-state",
        headers=ADMIN_HEADERS,
        params={"environment": "test-async", "backend_id": "composio"},
    )
    assert state.status_code == status.HTTP_200_OK, state.json()
    state_body = state.json()
    assert state_body["run_id"] == run_id
    assert state_body["last_status"] == "running"


@pytest.mark.anyio
async def test_builtins_sync_start_runs_inline_when_no_job(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestra.services import builtins_seed_launcher

    monkeypatch.setattr(
        builtins_seed_launcher,
        "builtins_seed_job_configured",
        lambda: False,
    )
    monkeypatch.delenv("ORCHESTRA_BUILTINS_SYNC_INLINE_ENABLED", raising=False)

    def fake_run(session_factory, request):  # noqa: ARG001
        return builtins_integration_sync.BuiltinsSyncResult(
            status="success",
            apps_upserted=3,
            tools_upserted=7,
            run_id=request.run_id,
            desired_hash=request.desired_hash,
        )

    monkeypatch.setattr(builtins_integration_sync, "run_builtins_sync", fake_run)

    response = await client.post(
        "/v0/admin/integrations/builtins-sync/start",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "composio",
            "environment": "test-inline",
            "desired_hash": "hash-inline-1",
            "cache_version": "cache-inline-1",
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    body = response.json()
    assert body["status"] == "success"
    assert body["apps_upserted"] == 3
    assert body["tools_upserted"] == 7
