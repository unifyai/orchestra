import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.models.integration_provider_models import IntegrationBackend
from orchestra.db.models.orchestra_models import Assistant, Context, Project, User
from orchestra.services.builtins_integration_sync import (
    BUILTINS_INTEGRATION_APPS_CONTEXT,
    BUILTINS_INTEGRATION_META_CONTEXT,
    BUILTINS_INTEGRATION_TOOLS_CONTEXT,
)
from orchestra.services.self_host_bootstrap import (
    bootstrap_self_host_platform,
    ensure_provider_integration_backends,
)


def test_self_host_bootstrap_seeds_platform_without_owner(dbsession: Session, _engine):
    before_users = dbsession.scalar(select(func.count()).select_from(User))
    before_coordinators = dbsession.scalar(
        select(func.count())
        .select_from(Assistant)
        .where(Assistant.is_coordinator.is_(True)),
    )

    result = bootstrap_self_host_platform(dbsession)

    after_users = dbsession.scalar(select(func.count()).select_from(User))
    after_coordinators = dbsession.scalar(
        select(func.count())
        .select_from(Assistant)
        .where(Assistant.is_coordinator.is_(True)),
    )

    assert result.ok is True
    assert after_users == before_users
    assert after_coordinators == before_coordinators

    builtins = dbsession.scalar(select(Project).where(Project.name == "Builtins"))
    assert builtins is not None
    assert builtins.user_id is None
    assert builtins.organization_id is None
    assert builtins.is_public_read is True
    assert builtins.is_system is True

    contexts = {
        row[0]
        for row in dbsession.execute(
            select(Context.name).where(Context.project_id == builtins.id),
        )
    }
    assert {
        BUILTINS_INTEGRATION_APPS_CONTEXT,
        BUILTINS_INTEGRATION_TOOLS_CONTEXT,
        BUILTINS_INTEGRATION_META_CONTEXT,
    }.issubset(contexts)


def test_ensure_provider_integration_backends_follows_credentials(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("PIPEDREAM_CLIENT_ID", raising=False)
    monkeypatch.delenv("PIPEDREAM_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PIPEDREAM_PROJECT_ID", raising=False)
    monkeypatch.delenv("SELF_HOST_PROVIDER_TRIGGERS_ENABLED", raising=False)

    ensure_provider_integration_backends(dbsession)
    dbsession.commit()

    composio = dbsession.scalar(
        select(IntegrationBackend).where(IntegrationBackend.backend_id == "composio"),
    )
    pipedream = dbsession.scalar(
        select(IntegrationBackend).where(IntegrationBackend.backend_id == "pipedream"),
    )
    assert composio is not None
    assert pipedream is not None
    assert composio.status == "disabled"
    assert pipedream.status == "disabled"

    monkeypatch.setenv("SELF_HOST_PROVIDER_TRIGGERS_ENABLED", "true")
    ensure_provider_integration_backends(dbsession)
    dbsession.commit()
    pipedream = dbsession.scalar(
        select(IntegrationBackend).where(IntegrationBackend.backend_id == "pipedream"),
    )
    assert pipedream is not None
    assert pipedream.status == "enabled"

    monkeypatch.setenv("PIPEDREAM_CLIENT_ID", "pd-client")
    monkeypatch.setenv("PIPEDREAM_CLIENT_SECRET", "pd-secret")
    monkeypatch.setenv("PIPEDREAM_PROJECT_ID", "pd-project")
    monkeypatch.setenv("COMPOSIO_API_KEY", "composio-key")
    ensure_provider_integration_backends(dbsession)
    dbsession.commit()
    composio = dbsession.scalar(
        select(IntegrationBackend).where(IntegrationBackend.backend_id == "composio"),
    )
    pipedream = dbsession.scalar(
        select(IntegrationBackend).where(IntegrationBackend.backend_id == "pipedream"),
    )
    assert composio is not None
    assert pipedream is not None
    assert composio.status == "enabled"
    assert pipedream.status == "enabled"
