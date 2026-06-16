"""DAO for integration backend, catalog, connection, policy, and audit rows.

The integration API has a fair amount of orchestration around provider
adapters, semantic indexing, and response shaping. Those concerns stay outside
the DAO. This class owns the SQLAlchemy-facing work so routes and service
facades do not hand-roll model queries.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session

from orchestra.db.models.integration_provider_models import (
    DynamicProviderApp,
    IntegrationBackend,
    IntegrationBootstrapState,
    IntegrationConnection,
    IntegrationOverlay,
    ProviderActionAudit,
    ProviderToolCatalog,
)

HIDDEN_CONNECTION_STATUSES = {"disconnected"}
PENDING_CONNECTION_TIMEOUT_SECONDS = int(
    os.getenv("INTEGRATION_PENDING_TIMEOUT_SECONDS", "1800"),
)


class IntegrationProviderDAO:
    """Persistence boundary for integration provider models."""

    def __init__(self, session: Session):
        self.session = session

    # Backends and seed catalog
    def seed_default_backends(self, *, default_backends: list[dict[str, Any]]) -> None:
        """Create missing backend rows without gating or seeding apps/tools.

        Backends are deployment configuration: operators can enable Composio,
        Pipedream, Unity Native, or any future provider by updating
        ``integration_backends.status`` through the admin API. Provider
        credentials/endpoints live in deployment environment variables;
        ``config_json`` is only for non-secret operational knobs. Apps and tools
        are provider catalog data and must arrive through sync endpoints, so
        this bootstrap intentionally does not create local sample apps/tools or
        allowlists.
        """

        for backend in default_backends:
            existing = (
                self.session.query(IntegrationBackend)
                .filter_by(backend_id=backend["backend_id"])
                .one_or_none()
            )
            if not existing:
                self.session.add(IntegrationBackend(**backend))
        self.session.flush()

    def list_backends(self) -> list[IntegrationBackend]:
        return (
            self.session.query(IntegrationBackend)
            .order_by(IntegrationBackend.default_priority.asc())
            .all()
        )

    def upsert_backend(self, values: dict[str, Any]) -> IntegrationBackend:
        existing = (
            self.session.query(IntegrationBackend)
            .filter_by(backend_id=values["backend_id"])
            .one_or_none()
        )
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            self.session.flush()
            return existing
        backend = IntegrationBackend(**values)
        self.session.add(backend)
        self.session.flush()
        return backend

    def patch_backend(
        self,
        backend_id: str,
        values: dict[str, Any],
    ) -> IntegrationBackend:
        backend = (
            self.session.query(IntegrationBackend)
            .filter_by(backend_id=backend_id)
            .one_or_none()
        )
        if not backend:
            raise ValueError(f"Unknown integration backend: {backend_id}")
        for key, value in values.items():
            setattr(backend, key, value)
        self.session.flush()
        return backend

    def get_backend(self, backend_id: str) -> IntegrationBackend | None:
        return (
            self.session.query(IntegrationBackend)
            .filter_by(backend_id=backend_id)
            .one_or_none()
        )

    def get_bootstrap_state(
        self,
        *,
        environment: str,
        backend_id: str,
    ) -> IntegrationBootstrapState | None:
        return (
            self.session.query(IntegrationBootstrapState)
            .filter_by(environment=environment, backend_id=backend_id)
            .one_or_none()
        )

    def list_bootstrap_states(
        self,
        *,
        environment: str | None = None,
    ) -> list[IntegrationBootstrapState]:
        query = self.session.query(IntegrationBootstrapState)
        if environment:
            query = query.filter_by(environment=environment)
        return query.order_by(
            IntegrationBootstrapState.environment.asc(),
            IntegrationBootstrapState.backend_id.asc(),
        ).all()

    def upsert_bootstrap_state(
        self,
        *,
        environment: str,
        backend_id: str,
        values: dict[str, Any],
    ) -> IntegrationBootstrapState:
        state = self.get_bootstrap_state(
            environment=environment,
            backend_id=backend_id,
        )
        if state:
            for key, value in values.items():
                setattr(state, key, value)
        else:
            state = IntegrationBootstrapState(
                environment=environment,
                backend_id=backend_id,
                **values,
            )
            self.session.add(state)
        self.session.flush()
        return state

    def active_backend_ids(self) -> set[str]:
        return {
            backend.backend_id
            for backend in self.session.query(IntegrationBackend)
            .filter_by(status="enabled")
            .all()
        }

    # App and tool catalog
    def upsert_catalog_app(
        self,
        *,
        backend_id: str,
        provider_app_id: str,
        canonical_app_slug: str,
        values: dict[str, Any],
    ) -> DynamicProviderApp:
        app = (
            self.session.query(DynamicProviderApp)
            .filter(
                DynamicProviderApp.backend_id == backend_id,
                or_(
                    DynamicProviderApp.provider_app_id == provider_app_id,
                    DynamicProviderApp.canonical_app_slug == canonical_app_slug,
                ),
            )
            .one_or_none()
        )
        if app:
            for key, value in values.items():
                setattr(app, key, value)
        else:
            app = DynamicProviderApp(**values)
            self.session.add(app)
        self.session.flush()
        return app

    def upsert_catalog_tool(
        self,
        *,
        tool_id: str,
        values: dict[str, Any],
    ) -> ProviderToolCatalog:
        tool = (
            self.session.query(ProviderToolCatalog)
            .filter_by(tool_id=tool_id)
            .one_or_none()
        )
        if tool:
            for key, value in values.items():
                setattr(tool, key, value)
        else:
            tool = ProviderToolCatalog(**values)
            self.session.add(tool)
        self.session.flush()
        return tool

    def prune_catalog_to_app_slugs(
        self,
        *,
        backend_id: str,
        canonical_app_slugs: Iterable[str],
    ) -> dict[str, int]:
        allowed = sorted({str(slug) for slug in canonical_app_slugs if str(slug)})
        tool_query = self.session.query(ProviderToolCatalog).filter_by(
            backend_id=backend_id,
        )
        app_query = self.session.query(DynamicProviderApp).filter_by(
            backend_id=backend_id,
        )
        if allowed:
            tool_query = tool_query.filter(
                ~ProviderToolCatalog.canonical_app_slug.in_(allowed),
            )
            app_query = app_query.filter(
                ~DynamicProviderApp.canonical_app_slug.in_(allowed),
            )
        tools_deleted = tool_query.delete(synchronize_session=False)
        apps_deleted = app_query.delete(synchronize_session=False)
        self.session.flush()
        return {
            "apps_pruned": int(apps_deleted),
            "tools_pruned": int(tools_deleted),
        }

    def delete_catalog_tools_for_app_slugs(
        self,
        *,
        backend_id: str,
        canonical_app_slugs: Iterable[str],
    ) -> int:
        slugs = sorted({str(slug) for slug in canonical_app_slugs if str(slug)})
        if not slugs:
            return 0
        deleted = (
            self.session.query(ProviderToolCatalog)
            .filter_by(backend_id=backend_id)
            .filter(ProviderToolCatalog.canonical_app_slug.in_(slugs))
            .delete(synchronize_session=False)
        )
        self.session.flush()
        return int(deleted)

    def get_app_by_backend_provider(
        self,
        *,
        backend_id: str,
        provider_app_id: str,
    ) -> DynamicProviderApp | None:
        return (
            self.session.query(DynamicProviderApp)
            .filter_by(backend_id=backend_id, provider_app_id=provider_app_id)
            .one_or_none()
        )

    def get_app_by_slug(
        self,
        canonical_app_slug: str,
        *,
        backend_id: str | None = None,
    ) -> DynamicProviderApp | None:
        query = self.session.query(DynamicProviderApp).filter_by(
            canonical_app_slug=canonical_app_slug,
        )
        if backend_id:
            query = query.filter_by(backend_id=backend_id)
        return query.order_by(DynamicProviderApp.backend_id.asc()).first()

    def set_app_action_previews(
        self,
        app: DynamicProviderApp,
        action_previews: list[dict[str, Any]],
    ) -> None:
        app.available_actions_json = action_previews
        self.session.flush()

    def catalog_apps_for_keys(
        self,
        keys: Iterable[tuple[str, str]],
    ) -> list[DynamicProviderApp]:
        apps: list[DynamicProviderApp] = []
        for backend_id, provider_app_id in keys:
            app = self.get_app_by_backend_provider(
                backend_id=backend_id,
                provider_app_id=provider_app_id,
            )
            if app:
                apps.append(app)
        return apps

    def catalog_tools_for_ids(
        self,
        tool_ids: Iterable[str],
    ) -> list[ProviderToolCatalog]:
        ids = list(dict.fromkeys(tool_ids))
        if not ids:
            return []
        return (
            self.session.query(ProviderToolCatalog)
            .filter(ProviderToolCatalog.tool_id.in_(ids))
            .order_by(ProviderToolCatalog.id.asc())
            .all()
        )

    def list_overlays_by_slug(self) -> dict[str, IntegrationOverlay]:
        return {
            overlay.canonical_app_slug: overlay
            for overlay in self.session.query(IntegrationOverlay).all()
        }

    def get_overlay(self, canonical_app_slug: str) -> IntegrationOverlay | None:
        return (
            self.session.query(IntegrationOverlay)
            .filter_by(canonical_app_slug=canonical_app_slug)
            .one_or_none()
        )

    def list_apps_by_slug(
        self,
        canonical_app_slugs: Iterable[str],
    ) -> dict[str, DynamicProviderApp]:
        slugs = list(dict.fromkeys(canonical_app_slugs))
        if not slugs:
            return {}
        return {
            app.canonical_app_slug: app
            for app in self.session.query(DynamicProviderApp)
            .filter(DynamicProviderApp.canonical_app_slug.in_(slugs))
            .order_by(DynamicProviderApp.backend_id.asc())
            .all()
        }

    def catalog_counts_by_backend(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for backend_id, count in (
            self.session.query(
                DynamicProviderApp.backend_id,
                func.count(DynamicProviderApp.id),
            )
            .group_by(DynamicProviderApp.backend_id)
            .all()
        ):
            counts.setdefault(backend_id, {})["apps"] = int(count)
        for backend_id, count in (
            self.session.query(
                ProviderToolCatalog.backend_id,
                func.count(ProviderToolCatalog.id),
            )
            .group_by(ProviderToolCatalog.backend_id)
            .all()
        ):
            counts.setdefault(backend_id, {})["tools"] = int(count)
        return counts

    def tool_count_for_app(self, canonical_app_slug: str) -> int:
        return (
            self.session.query(ProviderToolCatalog)
            .filter_by(canonical_app_slug=canonical_app_slug)
            .count()
        )

    def tool_counts_by_app(
        self,
        canonical_app_slugs: Iterable[str],
    ) -> dict[str, int]:
        slugs = list(dict.fromkeys(canonical_app_slugs))
        if not slugs:
            return {}
        return {
            slug: int(count)
            for slug, count in self.session.query(
                ProviderToolCatalog.canonical_app_slug,
                func.count(ProviderToolCatalog.id),
            )
            .filter(ProviderToolCatalog.canonical_app_slug.in_(slugs))
            .group_by(ProviderToolCatalog.canonical_app_slug)
            .all()
        }

    def list_tools(
        self,
        *,
        canonical_app_slug: str | None = None,
        backend_id: str | None = None,
    ) -> list[ProviderToolCatalog]:
        query = self._tools_query(
            canonical_app_slug=canonical_app_slug,
            backend_id=backend_id,
        )
        return query.order_by(
            ProviderToolCatalog.canonical_app_slug.asc(),
            ProviderToolCatalog.display_name.asc(),
        ).all()

    def _latest_owner_connections_subquery(self, owner: Any):
        row_number = (
            func.row_number()
            .over(
                partition_by=IntegrationConnection.canonical_app_slug,
                order_by=IntegrationConnection.updated_at.desc().nullslast(),
            )
            .label("row_number")
        )
        return (
            self.owner_filter(self.session.query(IntegrationConnection), owner)
            .filter(IntegrationConnection.status.notin_(HIDDEN_CONNECTION_STATUSES))
            .with_entities(
                IntegrationConnection.id.label("connection_pk"),
                IntegrationConnection.canonical_app_slug.label("canonical_app_slug"),
                IntegrationConnection.status.label("status"),
                IntegrationConnection.credential_storage.label("credential_storage"),
                IntegrationConnection.reconnect_reason.label("reconnect_reason"),
                IntegrationConnection.granted_scopes_json.label("granted_scopes_json"),
                IntegrationConnection.created_at.label("created_at"),
                IntegrationConnection.updated_at.label("updated_at"),
                row_number,
            )
            .subquery()
        )

    def _tools_query(
        self,
        *,
        canonical_app_slug: str | None = None,
        backend_id: str | None = None,
    ) -> Query:
        query = self.session.query(ProviderToolCatalog)
        if canonical_app_slug:
            query = query.filter_by(canonical_app_slug=canonical_app_slug)
        if backend_id:
            query = query.filter_by(backend_id=backend_id)
        return query

    def get_tool(self, tool_id: str) -> ProviderToolCatalog | None:
        return (
            self.session.query(ProviderToolCatalog)
            .filter_by(tool_id=tool_id)
            .one_or_none()
        )

    # Connections and policy state
    def owner_filter(self, query: Query, owner: Any) -> Query:
        query = query.filter(IntegrationConnection.owner_scope == owner.owner_scope)
        if owner.org_id is not None:
            query = query.filter(IntegrationConnection.org_id == owner.org_id)
        if owner.team_id is not None:
            query = query.filter(IntegrationConnection.team_id == owner.team_id)
        if owner.user_id:
            query = query.filter(IntegrationConnection.user_id == owner.user_id)
        if owner.assistant_id is not None:
            query = query.filter(
                IntegrationConnection.assistant_id == owner.assistant_id,
            )
        return query

    def expire_stale_pending_connection(self, conn: IntegrationConnection) -> bool:
        if conn.status != "pending":
            return False
        reference = conn.updated_at or conn.created_at
        if not reference:
            return False
        if datetime.utcnow() - reference.replace(tzinfo=None) <= timedelta(
            seconds=PENDING_CONNECTION_TIMEOUT_SECONDS,
        ):
            return False
        conn.status = "error"
        conn.reconnect_reason = "authorization_timeout"
        self.session.flush()
        return True

    def list_connections(
        self,
        owner: Any,
        *,
        include_disconnected: bool = False,
    ) -> list[IntegrationConnection]:
        query = self.owner_filter(self.session.query(IntegrationConnection), owner)
        if not include_disconnected:
            query = query.filter(
                IntegrationConnection.status.notin_(HIDDEN_CONNECTION_STATUSES),
            )
        connections = query.order_by(IntegrationConnection.updated_at.desc()).all()
        for conn in connections:
            self.expire_stale_pending_connection(conn)
        return connections

    def best_connection(
        self,
        *,
        owner: Any,
        canonical_app_slug: str,
        backend_id: str | None = None,
        connection_id: str | None = None,
    ) -> IntegrationConnection | None:
        query = self.session.query(IntegrationConnection)
        if connection_id:
            conn = (
                self.owner_filter(query, owner)
                .filter_by(connection_id=connection_id)
                .one_or_none()
            )
            if conn:
                self.expire_stale_pending_connection(conn)
            return conn
        query = self.owner_filter(query, owner).filter_by(
            canonical_app_slug=canonical_app_slug,
        )
        if backend_id:
            query = query.filter_by(backend_id=backend_id)
        query = query.filter(
            IntegrationConnection.status.notin_(HIDDEN_CONNECTION_STATUSES),
        )
        conn = query.order_by(IntegrationConnection.updated_at.desc()).first()
        if conn:
            self.expire_stale_pending_connection(conn)
        return conn

    def best_connections_by_app(
        self,
        *,
        owner: Any,
        canonical_app_slugs: Iterable[str],
    ) -> dict[str, IntegrationConnection]:
        slugs = list(dict.fromkeys(canonical_app_slugs))
        if not slugs:
            return {}
        row_number = (
            func.row_number()
            .over(
                partition_by=IntegrationConnection.canonical_app_slug,
                order_by=IntegrationConnection.updated_at.desc().nullslast(),
            )
            .label("row_number")
        )
        base_query = self.owner_filter(self.session.query(IntegrationConnection), owner)
        subquery = (
            base_query.filter(
                IntegrationConnection.canonical_app_slug.in_(slugs),
                IntegrationConnection.status.notin_(HIDDEN_CONNECTION_STATUSES),
            )
            .with_entities(IntegrationConnection.id.label("connection_pk"), row_number)
            .subquery()
        )
        connections = (
            self.session.query(IntegrationConnection)
            .join(subquery, IntegrationConnection.id == subquery.c.connection_pk)
            .filter(subquery.c.row_number == 1)
            .all()
        )
        result: dict[str, IntegrationConnection] = {}
        for conn in connections:
            self.expire_stale_pending_connection(conn)
            if conn.status not in HIDDEN_CONNECTION_STATUSES:
                result[conn.canonical_app_slug] = conn
        return result

    def create_connection(self, values: dict[str, Any]) -> IntegrationConnection:
        connection = IntegrationConnection(
            connection_id=f"ic_{uuid.uuid4().hex}",
            **values,
        )
        self.session.add(connection)
        self.session.flush()
        return connection

    def get_connection(self, connection_id: str) -> IntegrationConnection | None:
        return (
            self.session.query(IntegrationConnection)
            .filter_by(connection_id=connection_id)
            .one_or_none()
        )

    def find_connection_by_provider_id(
        self,
        *,
        provider_connection_id: str,
        owner: Any,
    ) -> IntegrationConnection | None:
        query = self.session.query(IntegrationConnection).filter_by(
            provider_connection_id=provider_connection_id,
        )
        return (
            self.owner_filter(query, owner)
            .filter(IntegrationConnection.status.notin_(HIDDEN_CONNECTION_STATUSES))
            .order_by(IntegrationConnection.updated_at.desc())
            .first()
        )

    def update_connection_fields(
        self,
        conn: IntegrationConnection,
        **values: Any,
    ) -> IntegrationConnection:
        for key, value in values.items():
            setattr(conn, key, value)
        self.session.flush()
        return conn

    def set_connection_tool_policy(
        self,
        conn: IntegrationConnection,
        policy: dict[str, Any],
    ) -> None:
        conn.disabled_actions_json = {"tool_policy": policy}
        self.session.flush()

    # Audits
    def add_action_audit(self, values: dict[str, Any]) -> ProviderActionAudit:
        audit = ProviderActionAudit(**values)
        self.session.add(audit)
        self.session.flush()
        return audit

    def get_action_audit(self, audit_id: int) -> ProviderActionAudit | None:
        return (
            self.session.query(ProviderActionAudit).filter_by(id=audit_id).one_or_none()
        )

    def update_action_audit(
        self,
        audit: ProviderActionAudit,
        **values: Any,
    ) -> ProviderActionAudit:
        for key, value in values.items():
            setattr(audit, key, value)
        self.session.flush()
        return audit
