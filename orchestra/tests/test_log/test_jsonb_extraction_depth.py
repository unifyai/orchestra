"""Tests for JSONB path depth gating in filter type inference.

Covers stale top-level FieldType names colliding with nested JSON paths: a flat
registry entry for ``category`` must not break filters on
``metadata.details.category`` when rows only store the nested value.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestra.db.models.core_models import LogEvent
from orchestra.web.api.log.python2SQL.helpers import (
    _infer_expression_type,
    _jsonb_extraction_depth,
)

from . import HEADERS, _create_log, _create_project


def _catalog_row(*, name: str, category: str, group_id: str = "group_one") -> dict:
    return {
        "name": name,
        "metadata": {
            "kind": "catalog",
            "details": {
                "group_id": group_id,
                "category": category,
                "record_id": f"{group_id}:{category}:{name}",
                "labels": {"display_name": category},
            },
        },
    }


def test_jsonb_extraction_depth_counts_grouped_nested_chains() -> None:
    data = LogEvent.data
    nested = data.op("->")("metadata").op("->")("details").op("->")("category")
    top_level = data.op("->")("payload")

    assert _jsonb_extraction_depth(nested) == 3
    assert _jsonb_extraction_depth(top_level) == 1


def test_nested_leaf_field_type_is_ignored_when_path_is_nested(monkeypatch) -> None:
    data = LogEvent.data
    nested = data.op("->")("metadata").op("->")("details").op("->")("category")

    def fake_get_field_type(key, session, project_id, context_id):
        return {"category": "str"}.get(key)

    monkeypatch.setattr(
        "orchestra.web.api.log.python2SQL.helpers._get_field_type_from_db",
        fake_get_field_type,
    )

    assert _infer_expression_type(nested, None, project_id=1, context_id=1) == "jsonb"


def test_top_level_field_type_is_used_for_single_hop_access(monkeypatch) -> None:
    data = LogEvent.data
    top_dict = data.op("->")("payload")
    top_list = data.op("->")("items")

    def fake_get_field_type(key, session, project_id, context_id):
        return {"payload": "dict", "items": "list", "category": "str"}.get(key)

    monkeypatch.setattr(
        "orchestra.web.api.log.python2SQL.helpers._get_field_type_from_db",
        fake_get_field_type,
    )

    assert _infer_expression_type(top_dict, None, project_id=1, context_id=1) == "dict"
    assert _infer_expression_type(top_list, None, project_id=1, context_id=1) == "list"


@pytest.mark.anyio
async def test_nested_category_filter_with_stale_top_level_field_type(
    client: AsyncClient,
) -> None:
    project_name = "test-nested-category-filter"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            _catalog_row(name="alpha_one", category="alpha"),
            _catalog_row(name="alpha_two", category="alpha"),
            _catalog_row(name="beta_one", category="beta", group_id="group_two"),
        ],
        params={},
    )

    cases = [
        (
            'metadata["kind"] == "catalog" '
            'and metadata["details"]["group_id"] == "group_one"',
            {"alpha_one", "alpha_two"},
        ),
        (
            'metadata["kind"] == "catalog" '
            'and metadata["details"]["category"] == "alpha"',
            {"alpha_one", "alpha_two"},
        ),
        (
            'metadata["kind"] == "catalog" '
            'and metadata["details"].get("category") == "alpha"',
            {"alpha_one", "alpha_two"},
        ),
        (
            'metadata["kind"] == "catalog" '
            'and metadata["details"]["record_id"].startswith("group_one:alpha:")',
            {"alpha_one", "alpha_two"},
        ),
        (
            'metadata["kind"] == "catalog" '
            'and metadata["details"]["labels"]["display_name"] == "alpha"',
            {"alpha_one", "alpha_two"},
        ),
    ]

    for filter, expected_names in cases:
        resp = await client.get(
            "/v0/logs",
            params={"project_name": project_name, "filter": filter},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        names = {log["entries"]["name"] for log in resp.json()["logs"]}
        assert names == expected_names, f"filter_expr={filter!r} got {names}"


@pytest.mark.anyio
async def test_stale_top_level_category_field_type_without_top_level_row_data(
    client: AsyncClient,
) -> None:
    """Registry still lists top-level ``category`` but rows store it nested only."""

    project_name = "test-stale-category-fieldtype"
    context_name = "tenant/42/Catalog/Items"
    await _create_project(client, project_name)

    fields_resp = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "context": context_name,
            "fields": {
                "category": {"type": "str", "mutable": True},
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
        entries=[_catalog_row(name="alpha_one", category="alpha")],
        params={},
    )

    legacy_resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": context_name,
            "filter": 'category == "alpha"',
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
            "filter": 'metadata["details"]["category"] == "alpha"',
        },
        headers=HEADERS,
    )
    assert nested_resp.status_code == 200, nested_resp.text
    names = {log["entries"]["name"] for log in nested_resp.json()["logs"]}
    assert names == {"alpha_one"}, names


@pytest.mark.anyio
async def test_top_level_category_row_does_not_break_nested_category_filter(
    client: AsyncClient,
) -> None:
    project_name = "test-category-fieldtype-collision"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {
                "name": "legacy-top-level-category",
                "category": {"canonical": "alpha", "source": "legacy"},
            },
            _catalog_row(name="alpha_one", category="alpha"),
        ],
        params={},
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter": 'metadata["details"]["category"] == "alpha"',
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    names = {log["entries"]["name"] for log in resp.json()["logs"]}
    assert names == {"alpha_one"}, names
