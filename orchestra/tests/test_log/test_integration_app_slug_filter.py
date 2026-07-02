"""Repro for metadata.integration.app_slug filter_expr on provider-backed function rows.

Mirrors staging Assistants/{user}/{assistant}/Functions/Primitives rows where:
- metadata.source == "provider_backed"
- metadata.integration.backend_id / tool_id / labels.* filter correctly
- metadata.integration.app_slug returns 0 rows despite being present in payloads
"""

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


def _gmail_tool_row(*, name_suffix: str = "patch_label") -> dict:
    return {
        "name": f"primitives.integrations.gmail.{name_suffix}",
        "description": "Gmail tool",
        "metadata": {
            "source": "provider_backed",
            "integration": {
                "backend_id": "composio",
                "app_slug": "gmail",
                "tool_id": f"composio:gmail:{name_suffix}",
                "labels": {"app_display_name": "gmail"},
            },
        },
    }


@pytest.mark.anyio
async def test_nested_integration_app_slug_filter_matches_provider_backed_rows(
    client: AsyncClient,
) -> None:
    project_name = "test-integration-app-slug-filter"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            _gmail_tool_row(name_suffix="patch_label"),
            _gmail_tool_row(name_suffix="send_email"),
            {
                "name": "primitives.integrations.slack.post_message",
                "description": "Slack tool",
                "metadata": {
                    "source": "provider_backed",
                    "integration": {
                        "backend_id": "composio",
                        "app_slug": "slack",
                        "tool_id": "composio:slack:post_message",
                        "labels": {"app_display_name": "slack"},
                    },
                },
            },
        ],
        params={},
    )

    cases = [
        (
            'metadata["source"] == "provider_backed" '
            'and metadata["integration"]["backend_id"] == "composio"',
            {
                "primitives.integrations.gmail.patch_label",
                "primitives.integrations.gmail.send_email",
                "primitives.integrations.slack.post_message",
            },
        ),
        (
            'metadata["source"] == "provider_backed" '
            'and metadata["integration"]["app_slug"] == "gmail"',
            {
                "primitives.integrations.gmail.patch_label",
                "primitives.integrations.gmail.send_email",
            },
        ),
        (
            'metadata["source"] == "provider_backed" '
            'and metadata["integration"].get("app_slug") == "gmail"',
            {
                "primitives.integrations.gmail.patch_label",
                "primitives.integrations.gmail.send_email",
            },
        ),
        (
            'metadata["source"] == "provider_backed" '
            'and metadata["integration"]["tool_id"].startswith("composio:gmail:")',
            {
                "primitives.integrations.gmail.patch_label",
                "primitives.integrations.gmail.send_email",
            },
        ),
        (
            'metadata["source"] == "provider_backed" '
            'and metadata["integration"]["labels"]["app_display_name"] == "gmail"',
            {
                "primitives.integrations.gmail.patch_label",
                "primitives.integrations.gmail.send_email",
            },
        ),
    ]

    for filter_expr, expected_names in cases:
        resp = await client.get(
            "/v0/logs",
            params={"project_name": project_name, "filter_expr": filter_expr},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        names = {log["entries"]["name"] for log in resp.json()["logs"]}
        assert names == expected_names, f"filter_expr={filter_expr!r} got {names}"


@pytest.mark.anyio
async def test_top_level_app_slug_field_type_does_not_break_nested_filter(
    client: AsyncClient,
) -> None:
    """If a context also has a top-level app_slug FieldType, nested filters must still work."""

    project_name = "test-integration-app-slug-fieldtype-collision"
    await _create_project(client, project_name)

    # Seed a row with a top-level app_slug key (different shape) first.
    await _create_log(
        client,
        project_name,
        entries=[
            {
                "name": "legacy-top-level-app-slug",
                "app_slug": {"canonical": "gmail", "provider": "composio"},
            },
            _gmail_tool_row(name_suffix="patch_label"),
        ],
        params={},
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": 'metadata["integration"]["app_slug"] == "gmail"',
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    names = {log["entries"]["name"] for log in resp.json()["logs"]}
    assert names == {"primitives.integrations.gmail.patch_label"}, names


@pytest.mark.anyio
async def test_stale_top_level_app_slug_field_type_without_top_level_row_data(
    client: AsyncClient,
) -> None:
    """Reproduces staging Assistants Functions/Primitives after unify metadata migration.

    Timeline on staging:
    - 2026-06-11: top-level ``app_slug`` FieldType registered (old row shape)
    - 2026-06-16: ``metadata`` FieldType registered; rows moved app_slug nested
    - Rows no longer store top-level app_slug, but FieldType registry still has it

    Raw SQL ``#>> '{metadata,integration,app_slug}'`` matches rows, but
    ``metadata["integration"]["app_slug"] == "gmail"`` must still work.
    """

    project_name = "test-stale-app-slug-fieldtype"
    context_name = "user/2103/Functions/Primitives"
    await _create_project(client, project_name)

    # Simulate the pre-migration field registry without keeping legacy row data.
    fields_resp = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "context": context_name,
            "fields": {
                "app_slug": {"type": "str", "mutable": True},
                "metadata": {"type": "dict", "mutable": True},
                "name": {"type": "str", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert fields_resp.status_code == 200, fields_resp.text

    await _create_log(
        client,
        project_name,
        context=context_name,
        entries=[_gmail_tool_row(name_suffix="patch_label")],
        params={},
    )

    legacy_resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": context_name,
            "filter_expr": 'app_slug == "gmail"',
        },
        headers=HEADERS,
    )
    assert legacy_resp.status_code == 200, legacy_resp.text
    assert legacy_resp.json()["logs"] == []

    nested_resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": context_name,
            "filter_expr": 'metadata["integration"]["app_slug"] == "gmail"',
        },
        headers=HEADERS,
    )
    assert nested_resp.status_code == 200, nested_resp.text
    names = {log["entries"]["name"] for log in nested_resp.json()["logs"]}
    assert names == {"primitives.integrations.gmail.patch_label"}, names
