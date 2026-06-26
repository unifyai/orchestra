from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, User
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
