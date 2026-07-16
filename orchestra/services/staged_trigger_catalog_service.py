"""Connection-gated provider trigger catalog from staged imports."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dao.trigger_catalog_dao import TriggerCatalogDAO
from orchestra.provider_triggers.catalog_import.registry import (
    supported_trigger_catalog_backends,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.provider_triggers.topology import evaluate_provider_trigger_topology
from orchestra.web.api.integrations.operations import OwnerContext

_ACTIVE_CONNECTION_STATUSES = frozenset({"connected", "active"})


def _catalog_environment() -> str:
    return os.getenv("PROVIDER_TRIGGER_CATALOG_ENVIRONMENT", "staging")


def _trigger_config_schema(
    *,
    backend_id: str,
    raw_metadata: dict[str, Any],
) -> dict[str, Any]:
    if backend_id == "composio":
        config = raw_metadata.get("config")
        return config if isinstance(config, dict) else {}
    if backend_id == "pipedream":
        return {
            "configurable_props": raw_metadata.get("configurable_props") or [],
            "description": raw_metadata.get("description"),
            "name": raw_metadata.get("name"),
        }
    return {}


def list_connected_app_slugs(
    session: Session,
    *,
    assistant_id: int,
    backend_id: str | None = None,
) -> set[tuple[str, str]]:
    """Return connected (backend_id, canonical_app_slug) pairs for one assistant."""

    dao = IntegrationProviderDAO(session)
    owner = OwnerContext(owner_scope="assistant", assistant_id=assistant_id)
    connections = dao.list_connections(owner)
    connected: set[tuple[str, str]] = set()
    for connection in connections:
        if connection.status not in {"connected", "active"}:
            continue
        if backend_id is not None and connection.backend_id != backend_id:
            continue
        if connection.canonical_app_slug:
            connected.add((connection.backend_id, connection.canonical_app_slug))
    return connected


def list_staged_triggers_for_assistant(
    session: Session,
    *,
    assistant_id: int,
    backend_id: str | None = None,
) -> dict[str, Any]:
    """Return staged provider triggers visible for one assistant's connections."""

    topology = evaluate_provider_trigger_topology(session, require_worker=False)
    connected_apps = list_connected_app_slugs(
        session,
        assistant_id=assistant_id,
        backend_id=backend_id,
    )
    if not topology.available:
        return {
            "available": False,
            "unavailable_reason": topology.unavailable_reason,
            "triggers": [],
        }

    catalog_dao = TriggerCatalogDAO(session)
    environment = _catalog_environment()
    triggers: list[dict[str, Any]] = []
    backends = (
        (backend_id,)
        if backend_id is not None
        else supported_trigger_catalog_backends()
    )
    for resolved_backend in backends:
        bootstrap = catalog_dao.get_bootstrap_state(
            environment=environment,
            backend_id=resolved_backend,
        )
        if bootstrap is None or not bootstrap.desired_hash:
            continue
        snapshot = catalog_dao.get_snapshot_by_hash(
            environment=environment,
            backend_id=resolved_backend,
            content_hash=bootstrap.desired_hash,
        )
        if snapshot is None:
            continue
        for candidate in catalog_dao.list_candidates_for_snapshot(snapshot.id):
            app_hint = (candidate.canonical_app_hint or "").strip().casefold()
            if not app_hint:
                continue
            if (resolved_backend, app_hint) not in {
                (backend, slug.casefold()) for backend, slug in connected_apps
            }:
                continue
            raw_metadata = dict(candidate.candidate_json.get("raw_metadata") or {})
            triggers.append(
                {
                    "backend_id": resolved_backend,
                    "canonical_app_slug": app_hint,
                    "provider_trigger_slug": candidate.provider_trigger_slug,
                    "provider_version": candidate.provider_version,
                    "display_name": raw_metadata.get("name"),
                    "description": raw_metadata.get("description"),
                    "config_schema": _trigger_config_schema(
                        backend_id=resolved_backend,
                        raw_metadata=raw_metadata,
                    ),
                },
            )

    triggers.sort(
        key=lambda item: (
            item["canonical_app_slug"],
            item["backend_id"],
            item["provider_trigger_slug"],
        ),
    )
    return {
        "available": True,
        "unavailable_reason": None,
        "triggers": triggers,
    }


def validate_provider_event_trigger_for_assistant(
    session: Session,
    *,
    assistant_id: int,
    trigger: ProviderEventTrigger,
) -> None:
    """Reject provider-event tasks that reference unknown connections or slugs."""

    owner = OwnerContext(owner_scope="assistant", assistant_id=assistant_id)
    connection = IntegrationProviderDAO(session).best_connection(
        owner=owner,
        canonical_app_slug=trigger.canonical_app_slug,
        backend_id=trigger.backend_id,
        connection_id=trigger.connection_id,
    )
    if connection is None:
        raise ValueError(
            f"provider_event_connection_not_found: {trigger.connection_id}",
        )
    if connection.status not in _ACTIVE_CONNECTION_STATUSES:
        raise ValueError(
            f"provider_event_connection_not_active: {trigger.connection_id}",
        )
    if connection.backend_id != trigger.backend_id:
        raise ValueError("provider_event_backend_mismatch")
    app_slug = (connection.canonical_app_slug or "").casefold()
    if app_slug != trigger.canonical_app_slug.casefold():
        raise ValueError("provider_event_app_mismatch")

    catalog = list_staged_triggers_for_assistant(
        session,
        assistant_id=assistant_id,
        backend_id=trigger.backend_id,
    )
    if not catalog.get("available"):
        raise ValueError("provider_event_catalog_unavailable")
    allowed_slugs = {
        row["provider_trigger_slug"]
        for row in catalog.get("triggers") or []
        if str(row.get("canonical_app_slug", "")).casefold() == app_slug
    }
    if trigger.provider_trigger_slug not in allowed_slugs:
        raise ValueError(
            "provider_event_trigger_slug_not_in_catalog: "
            f"{trigger.provider_trigger_slug}",
        )
