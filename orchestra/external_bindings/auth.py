"""Resolve ``auth_secret_ref`` from the tenant Secrets vault.

Bindings store only the secret *name*. Production resolution reads the
plaintext ``value`` from the owning ``…/Secrets`` context (team or assistant).
Process env is a last-resort fallback for local/dev.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from sqlalchemy import text

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.scope import OwnerScope, owner_from_context_name
from orchestra.external_bindings.types import ConnectorAuth

_SECRETS_SUFFIX = "Secrets"


def secrets_context_name_for_data_context(context_name: str) -> Optional[str]:
    """Map a data context name to its sibling Secrets vault path.

    * ``Teams/{team_id}/...`` → ``Teams/{team_id}/Secrets``
    * ``{user_id}/{agent_id}/...`` → ``{user_id}/{agent_id}/Secrets``
    * system / unrecognized → ``None`` (env fallback only)
    """
    parts = [p for p in (context_name or "").split("/") if p]
    if not parts:
        return None
    owner = owner_from_context_name(context_name)
    if owner.scope == OwnerScope.TEAM and owner.owner_id is not None:
        return f"Teams/{owner.owner_id}/{_SECRETS_SUFFIX}"
    if owner.scope == OwnerScope.ASSISTANT and owner.owner_id is not None:
        for i in range(1, len(parts)):
            if parts[i].isdigit() and not parts[i - 1].isdigit():
                return f"{parts[i - 1]}/{parts[i]}/{_SECRETS_SUFFIX}"
    return None


def lookup_secret_value(
    session,
    *,
    project_id: int,
    secrets_context_id: int,
    secret_name: str,
) -> Optional[str]:
    """Return ``value`` for the first Secrets log whose ``name`` matches."""
    row = session.execute(
        text(
            """
            SELECT le.data->>'value' AS secret_value
            FROM log_event le
            JOIN log_event_context lec
              ON le.id = lec.log_event_id
             AND le.project_id = lec.project_id
            WHERE le.project_id = :project_id
              AND lec.context_id = :secrets_context_id
              AND le.data->>'name' = :secret_name
            ORDER BY le.id DESC
            LIMIT 1
            """,
        ),
        {
            "project_id": project_id,
            "secrets_context_id": secrets_context_id,
            "secret_name": secret_name,
        },
    ).first()
    if row is None:
        return None
    value = row[0]
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def resolve_auth(
    binding: dict[str, Any],
    *,
    session=None,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
    cache: Optional[dict[tuple[Any, ...], Optional[str]]] = None,
    require: bool = False,
) -> ConnectorAuth:
    """Resolve ``auth_secret_ref`` from tenant Secrets, then process env.

    Parameters
    ----------
    binding:
        Binding body (may include ``auth_secret_ref``).
    session / project_id / context_id:
        When all three are set, look up the secret in the owning Secrets
        context derived from the *data* context at ``context_id``.
    cache:
        Optional per-request dict keyed by
        ``(project_id, secrets_context_id, ref)`` to avoid repeated vault
        queries during one hydrate batch.
    require:
        When True and ``auth_secret_ref`` is set but unresolved, raise
        ``ValueError`` with a clear vault path message.
    """
    ref = binding.get("auth_secret_ref")
    if not isinstance(ref, str) or not ref.strip():
        return ConnectorAuth(secret_value=None)
    ref = ref.strip()

    secret_value: Optional[str] = None
    secrets_path: Optional[str] = None

    if session is not None and project_id is not None and context_id is not None:
        data_rows = ContextDAO(session).filter(id=context_id, project_id=project_id)
        if data_rows:
            data_ctx = data_rows[0][0]
            secrets_path = secrets_context_name_for_data_context(data_ctx.name)
            if secrets_path:
                secrets_rows = ContextDAO(session).filter(
                    project_id=project_id,
                    name=secrets_path,
                )
                if secrets_rows:
                    secrets_ctx_id = int(secrets_rows[0][0].id)
                    cache_key = (project_id, secrets_ctx_id, ref)
                    if cache is not None and cache_key in cache:
                        secret_value = cache[cache_key]
                    else:
                        secret_value = lookup_secret_value(
                            session,
                            project_id=project_id,
                            secrets_context_id=secrets_ctx_id,
                            secret_name=ref,
                        )
                        if cache is not None:
                            cache[cache_key] = secret_value

    if not secret_value:
        secret_value = os.environ.get(ref) or None
        if isinstance(secret_value, str):
            secret_value = secret_value.strip() or None

    if require and not secret_value:
        where = secrets_path or "tenant Secrets (or process env)"
        raise ValueError(
            f"External auth secret '{ref}' not found in {where}",
        )

    return ConnectorAuth(secret_value=secret_value)
