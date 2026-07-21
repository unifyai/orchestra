"""Load workspace OAuth credentials for native trigger adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.provider_triggers.workspace_connection_facade import (
    is_workspace_facade_connection,
)


@dataclass(frozen=True)
class WorkspaceTriggerCredentials:
    """Resolved workspace OAuth credentials for one facade connection."""

    connection_id: str
    provider_connection_id: str
    account_email: str
    access_token: str
    refresh_token: str
    granted_scopes: tuple[str, ...]
    secret_values: Mapping[str, str]


class WorkspaceTriggerCredentialLoader:
    """Resolve assistant workspace secrets through a facade connection row."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._integration_dao = IntegrationProviderDAO(session)
        self._secret_dao = AssistantSecretDAO(session)

    def load_for_connection_id(
        self,
        connection_id: str,
    ) -> WorkspaceTriggerCredentials:
        connection = self._integration_dao.get_connection(connection_id)
        if connection is None:
            raise LookupError(f"Unknown integration connection {connection_id!r}")
        if not is_workspace_facade_connection(connection):
            raise ValueError(
                f"Connection {connection_id!r} is not a workspace trigger facade",
            )
        return self._load(connection)

    def _load(self, connection: IntegrationConnection) -> WorkspaceTriggerCredentials:
        if connection.assistant_id is None:
            raise ValueError("Workspace facade connection missing assistant_id")
        secret_refs = dict(connection.secret_refs_json or {})
        secret_values = {
            name: self._secret_dao.get(connection.assistant_id, secret_name) or ""
            for name, secret_name in secret_refs.items()
        }
        account_email = (
            secret_values.get("account_email") or connection.provider_user_id or ""
        ).strip()
        if not account_email:
            raise ValueError(
                f"Workspace facade {connection.connection_id!r} missing account email",
            )
        return WorkspaceTriggerCredentials(
            connection_id=connection.connection_id,
            provider_connection_id=connection.provider_connection_id or "",
            account_email=account_email,
            access_token=secret_values.get("access_token", ""),
            refresh_token=secret_values.get("refresh_token", ""),
            granted_scopes=tuple(connection.granted_scopes_json or []),
            secret_values=secret_values,
        )
