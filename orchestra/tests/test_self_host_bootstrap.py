from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, Context, Project, User
from orchestra.services.builtins_integration_sync import (
    BUILTINS_INTEGRATION_APPS_CONTEXT,
    BUILTINS_INTEGRATION_META_CONTEXT,
    BUILTINS_INTEGRATION_TOOLS_CONTEXT,
)
from orchestra.services.self_host_bootstrap import bootstrap_self_host_platform


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
