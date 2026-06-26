"""Bootstrap platform defaults for a self-host install.

Self-host user creation happens only through the Console UI. This module seeds
the non-user platform rows that a fresh local database needs before signup.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.web.api.integrations.operations import seed_default_provider_catalog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelfHostBootstrapResult:
    """Result printed after seeding self-host platform defaults."""

    ok: bool = True


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


def bootstrap_self_host_platform(session: Session) -> SelfHostBootstrapResult:
    """Create or repair self-host platform defaults without creating users."""
    ensure_platform_billing_defaults(session)
    ensure_provider_integration_backends(session)
    session.commit()
    return SelfHostBootstrapResult()


def run_self_host_bootstrap(session_factory: sessionmaker) -> SelfHostBootstrapResult:
    """Entry point for shell scripts."""
    session = session_factory()
    try:
        return bootstrap_self_host_platform(session)
    finally:
        session.close()
