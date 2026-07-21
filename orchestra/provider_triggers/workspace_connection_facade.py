"""Expose connected workspace OAuth as trigger-engine integration connections."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.provider_triggers.backend_ids import (
    ASSISTANT_WORKSPACE_SECRETS_STORAGE,
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_GOOGLE_CHAT_APP_SLUG,
    NATIVE_GOOGLE_DRIVE_APP_SLUG,
    NATIVE_GOOGLE_MEET_APP_SLUG,
    NATIVE_MICROSOFT_BACKEND_ID,
    NATIVE_MICROSOFT_DIRECTORY_APP_SLUG,
    NATIVE_MICROSOFT_GROUPS_APP_SLUG,
    NATIVE_MICROSOFT_ONEDRIVE_APP_SLUG,
    NATIVE_MICROSOFT_OUTLOOK_APP_SLUG,
    NATIVE_MICROSOFT_TEAMS_APP_SLUG,
    NATIVE_MICROSOFT_TODO_APP_SLUG,
)
from orchestra.web.api.integrations.operations import OwnerContext


@dataclass(frozen=True)
class _WorkspaceFacadeApp:
    backend_id: str
    canonical_app_slug: str
    provider_app_id: str
    scopes_secret: str
    account_email_secret: str
    secret_refs: dict[str, str]
    provider_connection_prefix: str
    # When non-empty, the facade connection only upserts as ``connected`` if the
    # granted scope set contains at least one of these scopes.
    required_any_scopes: frozenset[str] = frozenset()
    # When non-empty, every listed scope must be present (in addition to any
    # ``required_any_scopes`` check). Empty + empty ``required_any_scopes``
    # means the app is available whenever the workspace OAuth is connected.
    required_all_scopes: frozenset[str] = frozenset()


_GOOGLE_SECRET_REFS = {
    "access_token": "GOOGLE_ACCESS_TOKEN",
    "refresh_token": "GOOGLE_REFRESH_TOKEN",
    "token_expires_at": "GOOGLE_TOKEN_EXPIRES_AT",
    "granted_scopes": "GOOGLE_GRANTED_SCOPES",
    "account_email": "GOOGLE_ACCOUNT_EMAIL",
}

_MICROSOFT_SECRET_REFS = {
    "access_token": "MICROSOFT_ACCESS_TOKEN",
    "refresh_token": "MICROSOFT_REFRESH_TOKEN",
    "token_expires_at": "MICROSOFT_TOKEN_EXPIRES_AT",
    "granted_scopes": "MICROSOFT_GRANTED_SCOPES",
    "account_email": "MICROSOFT_ACCOUNT_EMAIL",
}


def _google_facade_apps(
    meet_event_scopes: frozenset[str],
    drive_event_scopes: frozenset[str],
    chat_event_scopes: frozenset[str],
) -> tuple[_WorkspaceFacadeApp, ...]:
    return (
        _WorkspaceFacadeApp(
            backend_id=NATIVE_GOOGLE_BACKEND_ID,
            canonical_app_slug=NATIVE_GOOGLE_MEET_APP_SLUG,
            provider_app_id="GOOGLE_MEET",
            scopes_secret="GOOGLE_GRANTED_SCOPES",
            account_email_secret="GOOGLE_ACCOUNT_EMAIL",
            secret_refs=dict(_GOOGLE_SECRET_REFS),
            provider_connection_prefix="google",
            required_any_scopes=meet_event_scopes,
        ),
        _WorkspaceFacadeApp(
            backend_id=NATIVE_GOOGLE_BACKEND_ID,
            canonical_app_slug=NATIVE_GOOGLE_DRIVE_APP_SLUG,
            provider_app_id="GOOGLE_DRIVE",
            scopes_secret="GOOGLE_GRANTED_SCOPES",
            account_email_secret="GOOGLE_ACCOUNT_EMAIL",
            secret_refs=dict(_GOOGLE_SECRET_REFS),
            provider_connection_prefix="google",
            required_any_scopes=drive_event_scopes,
        ),
        _WorkspaceFacadeApp(
            backend_id=NATIVE_GOOGLE_BACKEND_ID,
            canonical_app_slug=NATIVE_GOOGLE_CHAT_APP_SLUG,
            provider_app_id="GOOGLE_CHAT",
            scopes_secret="GOOGLE_GRANTED_SCOPES",
            account_email_secret="GOOGLE_ACCOUNT_EMAIL",
            secret_refs=dict(_GOOGLE_SECRET_REFS),
            provider_connection_prefix="google",
            required_all_scopes=chat_event_scopes,
        ),
    )


def _microsoft_facade_apps() -> tuple[_WorkspaceFacadeApp, ...]:
    return tuple(
        _WorkspaceFacadeApp(
            backend_id=NATIVE_MICROSOFT_BACKEND_ID,
            canonical_app_slug=slug,
            provider_app_id=provider_app_id,
            scopes_secret="MICROSOFT_GRANTED_SCOPES",
            account_email_secret="MICROSOFT_ACCOUNT_EMAIL",
            secret_refs=dict(_MICROSOFT_SECRET_REFS),
            provider_connection_prefix="microsoft",
        )
        for slug, provider_app_id in (
            (NATIVE_MICROSOFT_TEAMS_APP_SLUG, "MICROSOFT_TEAMS"),
            (NATIVE_MICROSOFT_OUTLOOK_APP_SLUG, "MICROSOFT_OUTLOOK"),
            (NATIVE_MICROSOFT_ONEDRIVE_APP_SLUG, "MICROSOFT_ONEDRIVE"),
            (NATIVE_MICROSOFT_GROUPS_APP_SLUG, "MICROSOFT_GROUPS"),
            (NATIVE_MICROSOFT_DIRECTORY_APP_SLUG, "MICROSOFT_DIRECTORY"),
            (NATIVE_MICROSOFT_TODO_APP_SLUG, "MICROSOFT_TODO"),
        )
    )


@lru_cache(maxsize=1)
def _workspace_facade_apps() -> tuple[_WorkspaceFacadeApp, ...]:
    # Imported lazily: ``scopes`` lives under the ``orchestra.web.api.assistant``
    # package whose ``__init__`` pulls in views → services → this module, so a
    # top-level import here would be circular. At call time everything is loaded.
    from orchestra.web.api.assistant.scopes import (
        GOOGLE_CHAT_EVENT_SCOPES,
        GOOGLE_DRIVE_EVENT_SCOPES,
        GOOGLE_MEET_EVENT_SCOPES,
    )

    return (
        *_google_facade_apps(
            GOOGLE_MEET_EVENT_SCOPES,
            GOOGLE_DRIVE_EVENT_SCOPES,
            GOOGLE_CHAT_EVENT_SCOPES,
        ),
        *_microsoft_facade_apps(),
    )


def ensure_workspace_trigger_connections(
    session: Session,
    *,
    assistant_id: int,
) -> list[IntegrationConnection]:
    """Upsert or deactivate workspace-backed trigger facade connections."""

    owner = OwnerContext(owner_scope="assistant", assistant_id=assistant_id)
    secret_dao = AssistantSecretDAO(session)
    integration_dao = IntegrationProviderDAO(session)
    secrets = secret_dao.get_all(assistant_id)
    updated: list[IntegrationConnection] = []

    for app in _workspace_facade_apps():
        connection = _find_facade_connection(
            integration_dao,
            owner=owner,
            app=app,
        )
        scopes = (secrets.get(app.scopes_secret) or "").strip()
        account_email = (secrets.get(app.account_email_secret) or "").strip().lower()
        granted_scope_set = set(scopes.split())
        has_required_scopes = True
        if app.required_any_scopes:
            has_required_scopes = bool(
                app.required_any_scopes & granted_scope_set,
            )
        if app.required_all_scopes:
            has_required_scopes = (
                has_required_scopes
                and app.required_all_scopes.issubset(granted_scope_set)
            )
        if scopes and account_email and has_required_scopes:
            values = _facade_connection_values(
                assistant_id=assistant_id,
                app=app,
                account_email=account_email,
                granted_scopes=scopes.split(),
            )
            if connection is None:
                connection = IntegrationConnection(**values)
                session.add(connection)
            else:
                integration_dao.update_connection_fields(connection, **values)
            updated.append(connection)
            continue

        if connection is not None and connection.status != "disconnected":
            integration_dao.update_connection_fields(connection, status="disconnected")
            updated.append(connection)

    session.flush()
    return updated


def deactivate_workspace_trigger_connections(
    session: Session,
    *,
    assistant_id: int,
) -> None:
    """Mark all workspace-backed trigger facade rows disconnected."""

    owner = OwnerContext(owner_scope="assistant", assistant_id=assistant_id)
    integration_dao = IntegrationProviderDAO(session)
    for app in _workspace_facade_apps():
        connection = _find_facade_connection(
            integration_dao,
            owner=owner,
            app=app,
        )
        if connection is not None and connection.status != "disconnected":
            integration_dao.update_connection_fields(connection, status="disconnected")
    session.flush()


def is_workspace_facade_connection(connection: IntegrationConnection) -> bool:
    """Return True when *connection* is a workspace-backed trigger facade row."""

    return connection.credential_storage == ASSISTANT_WORKSPACE_SECRETS_STORAGE


def _find_facade_connection(
    integration_dao: IntegrationProviderDAO,
    *,
    owner: OwnerContext,
    app: _WorkspaceFacadeApp,
) -> IntegrationConnection | None:
    for connection in integration_dao.list_connections(
        owner,
        include_disconnected=True,
    ):
        if (
            connection.backend_id == app.backend_id
            and connection.canonical_app_slug == app.canonical_app_slug
            and connection.credential_storage == ASSISTANT_WORKSPACE_SECRETS_STORAGE
        ):
            return connection
    return None


def _facade_connection_values(
    *,
    assistant_id: int,
    app: _WorkspaceFacadeApp,
    account_email: str,
    granted_scopes: list[str],
) -> dict[str, Any]:
    provider_connection_id = f"{app.provider_connection_prefix}:{account_email}"
    return {
        "connection_id": _facade_connection_id(
            assistant_id=assistant_id,
            backend_id=app.backend_id,
            canonical_app_slug=app.canonical_app_slug,
        ),
        "owner_scope": "assistant",
        "assistant_id": assistant_id,
        "canonical_app_slug": app.canonical_app_slug,
        "backend_id": app.backend_id,
        "provider_app_id": app.provider_app_id,
        "provider_connection_id": provider_connection_id,
        "provider_user_id": account_email,
        "status": "connected",
        "external_account_label": account_email,
        "granted_scopes_json": granted_scopes,
        "credential_storage": ASSISTANT_WORKSPACE_SECRETS_STORAGE,
        "secret_refs_json": dict(app.secret_refs),
    }


def _facade_connection_id(
    *,
    assistant_id: int,
    backend_id: str,
    canonical_app_slug: str,
) -> str:
    return f"ic_ws_{backend_id}_{canonical_app_slug}_{assistant_id}"
