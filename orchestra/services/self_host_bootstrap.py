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
from orchestra.db.dao.onboarding_status_dao import OnboardingStatusDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder
from orchestra.services.coordinator_service import (
    ensure_personal_coordinator_provisioned,
    get_personal_coordinator,
)
from orchestra.web.api.users.views import generate_key

logger = logging.getLogger(__name__)

ph = PasswordHasher()

SELF_HOST_OWNER_EMAIL = "owner@selfhost.dev"
SELF_HOST_OWNER_NAME = "Local Owner"
SELF_HOST_DEFAULT_VOICE_PROVIDER = "cartesia"
# Cartesia preset voice
SELF_HOST_DEFAULT_VOICE_ID = "bf0a246a-8642-498a-9950-80c35e9276b5"
SELF_HOST_DEFAULT_VOICE_NAME = "English Woman Calm 1"
SELF_HOST_DEFAULT_VOICE_DESCRIPTION = (
    "Calm and relaxing voice of an english-speaking woman"
)


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


def _apply_self_host_coordinator_voice(
    session: Session,
    *,
    user_id: str,
    coordinator_agent_id: int,
) -> None:
    """Register a Cartesia preset voice and attach it to the Coordinator."""
    provider = (
        os.environ.get("SELF_HOST_DEFAULT_VOICE_PROVIDER", "").strip()
        or SELF_HOST_DEFAULT_VOICE_PROVIDER
    )
    voice_id = (
        os.environ.get("SELF_HOST_DEFAULT_VOICE_ID", "").strip()
        or SELF_HOST_DEFAULT_VOICE_ID
    )
    if not voice_id:
        logger.warning(
            "Skipping Coordinator voice setup: SELF_HOST_DEFAULT_VOICE_ID is empty",
        )
        return

    session.execute(
        text(
            """
            INSERT INTO voices (
                voice_id, user_id, name, description, gender, language, is_preset, provider
            )
            VALUES (
                :voice_id, :user_id, :name, :description, :gender, :language, true, :provider
            )
            ON CONFLICT DO NOTHING
            """,
        ),
        {
            "voice_id": voice_id,
            "user_id": user_id,
            "name": SELF_HOST_DEFAULT_VOICE_NAME,
            "description": SELF_HOST_DEFAULT_VOICE_DESCRIPTION,
            "gender": "female",
            "language": "en",
            "provider": provider,
        },
    )
    session.execute(
        text(
            """
            UPDATE assistants
            SET voice_provider = :provider, voice_id = :voice_id
            WHERE agent_id = :agent_id AND is_coordinator IS TRUE
            """,
        ),
        {
            "provider": provider,
            "voice_id": voice_id,
            "agent_id": coordinator_agent_id,
        },
    )
    session.flush()


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
        _apply_self_host_coordinator_voice(
            session,
            user_id=str(user.id),
            coordinator_agent_id=coordinator.agent_id,
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
