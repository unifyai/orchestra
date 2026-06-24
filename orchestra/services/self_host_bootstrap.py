"""Bootstrap a single self-host owner and personal Coordinator.

Uses the same Orchestra provisioning path as production signup
(``ensure_personal_coordinator_provisioned``), not Console seed SQL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.auth_dao import AuthDAO
from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dao.onboarding_status_dao import OnboardingStatusDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder
from orchestra.services.coordinator_service import (
    ensure_personal_coordinator_provisioned,
    get_personal_coordinator,
)
from orchestra.web.api.integrations.operations import seed_default_provider_catalog
from orchestra.web.api.users.views import generate_key

logger = logging.getLogger(__name__)

ph = PasswordHasher()

SELF_HOST_OWNER_EMAIL = "owner@selfhost.dev"
SELF_HOST_OWNER_NAME = "Local Owner"


@dataclass(frozen=True)
class SelfHostBootstrapResult:
    """Credentials and runtime ids printed after ``unity stack up``."""

    user_id: str
    email: str
    password: str
    api_key: str
    coordinator_agent_id: int
    created_user: bool
    created_coordinator: bool


def ensure_platform_billing_defaults(session: Session) -> None:
    """Seed rows a fresh local Postgres volume needs before ``UserDAO.create``."""
    session.execute(
        text(
            """
            INSERT INTO plan_group (id, name, display_name, description, is_active)
            VALUES (1, 'default', 'Default', 'Default plan group for self-host', true)
            ON CONFLICT (id) DO NOTHING;
            """,
        ),
    )
    session.execute(
        text(
            """
            INSERT INTO billing_plan_template (
                id, name, display_name, billing_mode, is_custom, is_active
            )
            VALUES (1, 'default', 'Default', 'CREDITS', false, true)
            ON CONFLICT (id) DO NOTHING;
            """,
        ),
    )
    session.execute(
        text(
            "SELECT setval('plan_group_id_seq', GREATEST((SELECT MAX(id) FROM plan_group), 1));",
        ),
    )
    session.execute(
        text(
            """
            SELECT setval(
                'billing_plan_template_id_seq',
                GREATEST((SELECT MAX(id) FROM billing_plan_template), 1)
            );
            """,
        ),
    )
    session.flush()


def ensure_provider_integration_backends(session: Session) -> None:
    """Align integration backend status with the configured provider credentials.

    Composio executes live only when ``COMPOSIO_API_KEY`` is configured, so the
    backend row is enabled exactly when the key is present and disabled
    otherwise. Provider catalog normalization stays with the admin bootstrap
    script, and the compose stack then feeds its snapshot into Unity's Builtins
    seeder so public app/tool discovery uses the shared Builtins project.
    """
    seed_default_provider_catalog(session)
    status = "enabled" if os.environ.get("COMPOSIO_API_KEY", "").strip() else "disabled"
    IntegrationProviderDAO(session).patch_backend("composio", {"status": status})
    session.flush()
    logger.info("Composio integration backend %s for self-host", status)


def _resolve_password() -> str:
    configured = os.environ.get("SELF_HOST_OWNER_PASSWORD", "").strip()
    if configured:
        return configured

    credentials_path = os.path.expanduser(
        os.environ.get(
            "SELF_HOST_CREDENTIALS_FILE",
            "~/.unity/self-host-credentials.json",
        ),
    )
    if os.path.isfile(credentials_path):
        import json

        with open(credentials_path, encoding="utf-8") as fh:
            saved = json.load(fh).get("password", "").strip()
        if saved:
            return saved

    generated = secrets.token_urlsafe(16)
    logger.info(
        "Generated self-host owner password (set SELF_HOST_OWNER_PASSWORD to pin it)",
    )
    return generated


async def bootstrap_self_host_owner(session: Session) -> SelfHostBootstrapResult:
    """Create or repair the self-host owner account and personal Coordinator."""
    ensure_platform_billing_defaults(session)

    email = SELF_HOST_OWNER_EMAIL.lower().strip()
    password = _resolve_password()
    user_dao = UserDAO(session)
    existing_rows = user_dao.filter(email=email)

    if existing_rows:
        user = existing_rows[0][0]
        created_user = False
        auth_dao = AuthDAO(session)
        credentials = auth_dao.get_email_credentials(user.id)
        password_hash = ph.hash(password)
        if credentials is not None:
            credentials.password_hash = password_hash
        else:
            auth_dao.create_email_credentials(
                user_id=user.id,
                password_hash=password_hash,
                email_verified=True,
            )
        onboarding_dao = OnboardingStatusDAO(session)
        if onboarding_dao.get_by_user_id(user.id) is None:
            onboarding_dao.create(
                user_id=user.id,
                current_step="workspace_setup",
            )
        api_key_dao = ApiKeyDAO(session)
        key_rows = api_key_dao.filter(user_id=user.id)
        if key_rows:
            api_key = key_rows[0][0].key
        else:
            api_key = generate_key()
            api_key_dao.create(key=api_key, name="self-host", user_id=user.id)
    else:
        created_user = True
        user = user_dao.create(
            email=email,
            name=SELF_HOST_OWNER_NAME,
            last_name="",
        )
        session.flush()
        api_key = generate_key()
        ApiKeyDAO(session).create(key=api_key, name="self-host", user_id=user.id)
        try:
            DefaultTasksSeeder.seed(session, user_id=str(user.id))
        except Exception as exc:
            logger.warning("Default task seed skipped for self-host owner: %s", exc)
        AuthDAO(session).create_email_credentials(
            user_id=user.id,
            password_hash=ph.hash(password),
            email_verified=True,
        )
        OnboardingStatusDAO(session).create(
            user_id=user.id,
            current_step="workspace_setup",
        )

    session.commit()

    created_coordinator = False
    try:
        coordinator, created_coordinator = (
            await ensure_personal_coordinator_provisioned(
                session,
                user_id=str(user.id),
            )
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        coordinator = get_personal_coordinator(session, str(user.id))
        if coordinator is None:
            raise
        logger.warning(
            "Coordinator provisioning skipped during self-host bootstrap repair: %s",
            exc,
        )

    return SelfHostBootstrapResult(
        user_id=str(user.id),
        email=email,
        password=password,
        api_key=api_key,
        coordinator_agent_id=coordinator.agent_id,
        created_user=created_user,
        created_coordinator=created_coordinator,
    )


def run_self_host_bootstrap(session_factory: sessionmaker) -> SelfHostBootstrapResult:
    """Entry point for shell scripts."""
    session = session_factory()
    try:
        return asyncio.run(bootstrap_self_host_owner(session))
    finally:
        session.close()
