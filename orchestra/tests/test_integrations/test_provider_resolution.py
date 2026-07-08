"""Tests for cross-provider integration app resolution."""

from __future__ import annotations

from pathlib import Path

from orchestra.integrations.provider_resolution import (
    CatalogAppRef,
    composio_covered_keys,
    filter_pipedream_app_entries,
    load_pipedream_allowlist,
    logical_app_key,
    resolve_public_catalog_apps,
    resolve_public_catalog_tools,
    should_sync_pipedream_app,
    slug_variants,
)


def test_slug_variants_include_microsoft_and_oauth_aliases() -> None:
    assert "outlook" in slug_variants("microsoft_outlook")
    assert "airtable" in slug_variants("airtable_oauth")


def test_logical_app_key_normalizes_microsoft_outlook_to_outlook() -> None:
    assert (
        logical_app_key(
            canonical_app_slug="microsoft_outlook",
            display_name="Microsoft Outlook",
        )
        == "outlook"
    )


def test_should_sync_pipedream_app_requires_allowlist_and_no_composio_overlap() -> None:
    composio_keys = composio_covered_keys(
        [
            {
                "backend_id": "composio",
                "canonical_app_slug": "outlook",
                "display_name": "Outlook",
            },
        ],
    )
    allowlist = {"microsoft_outlook", "acuity_scheduling"}
    assert not should_sync_pipedream_app(
        {
            "backend_id": "pipedream",
            "canonical_app_slug": "microsoft_outlook",
            "display_name": "Microsoft Outlook",
        },
        composio_keys=composio_keys,
        allowlist=allowlist,
    )
    assert should_sync_pipedream_app(
        {
            "backend_id": "pipedream",
            "canonical_app_slug": "acuity_scheduling",
            "display_name": "Acuity Scheduling",
        },
        composio_keys=composio_keys,
        allowlist=allowlist,
    )


def test_filter_pipedream_app_entries_drops_composio_covered_apps() -> None:
    composio_entries = [
        {
            "backend_id": "composio",
            "canonical_app_slug": "slack",
            "display_name": "Slack",
        },
    ]
    pipedream_entries = [
        {
            "backend_id": "pipedream",
            "canonical_app_slug": "slack",
            "display_name": "Slack",
        },
        {
            "backend_id": "pipedream",
            "canonical_app_slug": "acuity_scheduling",
            "display_name": "Acuity Scheduling",
        },
    ]
    filtered = filter_pipedream_app_entries(
        pipedream_entries,
        composio_entries=composio_entries,
        allowlist={"slack", "acuity_scheduling"},
    )
    assert [row["canonical_app_slug"] for row in filtered] == ["acuity_scheduling"]


def test_resolve_public_catalog_apps_prefers_composio() -> None:
    apps = [
        {
            "backend_id": "pipedream",
            "canonical_app_slug": "github",
            "display_name": "GitHub",
        },
        {
            "backend_id": "composio",
            "canonical_app_slug": "github",
            "display_name": "GitHub",
        },
    ]
    resolved = resolve_public_catalog_apps(apps)
    assert len(resolved) == 1
    assert resolved[0]["backend_id"] == "composio"


def test_catalog_app_ref_logical_key_uses_display_name_when_needed() -> None:
    ref = CatalogAppRef.from_entry(
        {
            "backend_id": "pipedream",
            "canonical_app_slug": "app_x1",
            "display_name": "Acuity Scheduling",
        },
    )
    assert ref.logical_key == "acuity_scheduling"


def test_load_pipedream_allowlist_reads_root_and_legacy_metadata_tables(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "root.toml"
    root_path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                'app_slugs = ["ably", "0codekit"]',
                "",
                "[metadata]",
                "allowlist_count = 2",
            ],
        ),
        encoding="utf-8",
    )
    assert load_pipedream_allowlist(root_path) == {"ably", "0codekit"}

    legacy_path = tmp_path / "legacy.toml"
    legacy_path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[metadata]",
                "allowlist_count = 1",
                'app_slugs = ["apollo_io"]',
            ],
        ),
        encoding="utf-8",
    )
    assert load_pipedream_allowlist(legacy_path) == {"apollo_io"}


def _tool_row(
    *,
    backend_id: str,
    app_slug: str,
    tool_name: str,
) -> dict:
    return {
        "name": f"primitives.integrations.{app_slug}.{tool_name}",
        "metadata": {
            "integration": {
                "backend_id": backend_id,
                "app_slug": app_slug,
                "provider_tool_id": tool_name,
            },
        },
    }


def test_resolve_public_catalog_tools_prefers_composio() -> None:
    tools = [
        _tool_row(backend_id="pipedream", app_slug="slack", tool_name="send_message"),
        _tool_row(backend_id="composio", app_slug="slack", tool_name="send_message"),
    ]
    resolved = resolve_public_catalog_tools(tools)
    assert len(resolved) == 1
    assert resolved[0]["metadata"]["integration"]["backend_id"] == "composio"
