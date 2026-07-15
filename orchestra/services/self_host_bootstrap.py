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
from orchestra.db.models.core_models import Project
from orchestra.services.builtins_integration_sync import (
    BUILTINS_PROJECT_NAME,
    ensure_builtins_catalog_contexts,
)
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
    otherwise. Pipedream Connect executes live only when client credentials and
    a project id are all configured. Provider catalog normalization stays with
    the admin bootstrap script, and the compose stack then feeds its snapshot
    into Unity's Builtins seeder so public app/tool discovery uses the shared
    Builtins project.
    """
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    composio_status = (
        "enabled" if os.environ.get("COMPOSIO_API_KEY", "").strip() else "disabled"
    )
    dao.patch_backend("composio", {"status": composio_status})
    pipedream_configured = all(
        os.environ.get(name, "").strip()
        for name in (
            "PIPEDREAM_CLIENT_ID",
            "PIPEDREAM_CLIENT_SECRET",
            "PIPEDREAM_PROJECT_ID",
        )
    )
    provider_triggers_enabled = os.environ.get(
        "SELF_HOST_PROVIDER_TRIGGERS_ENABLED",
        "",
    ).strip().lower() in {"1", "true", "yes", "on"}
    pipedream_status = (
        "enabled" if pipedream_configured or provider_triggers_enabled else "disabled"
    )
    dao.patch_backend("pipedream", {"status": pipedream_status})
    session.flush()
    logger.info("Composio integration backend %s for self-host", composio_status)
    logger.info("Pipedream integration backend %s for self-host", pipedream_status)


def ensure_system_builtins_project(session: Session) -> Project:
    """Ensure the canonical Builtins project exists as platform-owned data."""
    project = (
        session.query(Project)
        .filter(Project.name == BUILTINS_PROJECT_NAME)
        .order_by(
            Project.is_system.desc(),
            Project.is_public_read.desc(),
            Project.id.asc(),
        )
        .first()
    )
    if project is None:
        project = Project(
            name=BUILTINS_PROJECT_NAME,
            description="System Builtins catalogue",
            is_versioned=True,
            is_public_read=True,
            is_system=True,
        )
        session.add(project)
        session.flush()
    else:
        project.user_id = None
        project.organization_id = None
        project.is_versioned = True
        project.is_public_read = True
        project.is_system = True
        if not project.description:
            project.description = "System Builtins catalogue"
        session.flush()
    return project


def ensure_system_assistant_jobs_project(session: Session) -> Project:
    """Ensure the canonical AssistantJobs project exists as platform-owned data."""
    from orchestra.web.api.utils.system_project import ASSISTANT_JOBS_PROJECT_NAME

    project = (
        session.query(Project)
        .filter(Project.name == ASSISTANT_JOBS_PROJECT_NAME)
        .order_by(
            Project.is_system.desc(),
            Project.id.asc(),
        )
        .first()
    )
    if project is None:
        project = Project(
            name=ASSISTANT_JOBS_PROJECT_NAME,
            description="Platform fleet audit and Console liveview discovery",
            is_versioned=False,
            is_public_read=False,
            is_system=True,
        )
        session.add(project)
        session.flush()
    else:
        project.user_id = None
        project.organization_id = None
        project.is_versioned = False
        project.is_public_read = False
        project.is_system = True
        if not project.description:
            project.description = "Platform fleet audit and Console liveview discovery"
        session.flush()
    return project


def bootstrap_self_host_platform(session: Session) -> SelfHostBootstrapResult:
    """Create or repair self-host platform defaults without creating users."""
    ensure_platform_billing_defaults(session)
    ensure_provider_integration_backends(session)
    ensure_system_builtins_project(session)
    ensure_system_assistant_jobs_project(session)
    ensure_builtins_catalog_contexts(session)
    session.commit()
    return SelfHostBootstrapResult()


def run_self_host_bootstrap(session_factory: sessionmaker) -> SelfHostBootstrapResult:
    """Entry point for shell scripts."""
    session = session_factory()
    try:
        return bootstrap_self_host_platform(session)
    finally:
        session.close()
