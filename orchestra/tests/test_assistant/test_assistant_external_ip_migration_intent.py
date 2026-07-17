"""Tests for durable assistant IP placement intent."""

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantExternalIP,
    AssistantExternalIPHistory,
    AssistantExternalIPRegionalMigration,
    User,
)
from orchestra.services.assistant_external_ip_service import (
    record_assistant_external_ip_regional_migration_intent,
)


def _make_assistant_with_external_ip(
    dbsession: Session,
) -> tuple[Assistant, AssistantExternalIP]:
    user = User(id="regional-migration-user", email="regional-migration@test.com")
    dbsession.add(user)
    dbsession.flush()
    assistant = Assistant(user_id=user.id, first_name="Placement", surname="Test")
    external_ip = AssistantExternalIP(
        assistant=assistant,
        gcp_address_name="placement-address",
        address="203.0.113.10",
        pool_location="us-central",
        region="us-central1",
        state="reserved",
    )
    dbsession.add_all((assistant, external_ip))
    dbsession.flush()
    return assistant, external_ip


def test_regional_migration_intent_preserves_active_allocation(
    dbsession: Session,
) -> None:
    assistant, external_ip = _make_assistant_with_external_ip(dbsession)

    operation = record_assistant_external_ip_regional_migration_intent(
        dbsession,
        assistant=assistant,
        desired_pool_location="europe-west",
        requested_timezone="Europe/London",
    )
    dbsession.flush()

    assert operation is not None
    assert operation.state == "requested"
    assert operation.source_pool_location == "us-central"
    assert operation.source_region == "us-central1"
    assert operation.requested_timezone == "Europe/London"
    assert external_ip.pool_location == "us-central"
    assert external_ip.region == "us-central1"
    assert external_ip.address == "203.0.113.10"
    assert external_ip.active_operation is None
    assert external_ip.desired_pool_location == "europe-west"
    assert (
        dbsession.query(AssistantExternalIPRegionalMigration)
        .filter_by(external_ip_id=external_ip.id)
        .count()
        == 1
    )
    history = (
        dbsession.query(AssistantExternalIPHistory)
        .filter_by(external_ip_id=external_ip.id)
        .one()
    )
    assert history.operation == "regional_migration_requested"
    assert history.details["operation_id"] == operation.id


def test_regional_migration_intent_is_idempotent_for_target(
    dbsession: Session,
) -> None:
    assistant, external_ip = _make_assistant_with_external_ip(dbsession)

    first = record_assistant_external_ip_regional_migration_intent(
        dbsession,
        assistant=assistant,
        desired_pool_location="asia-east",
    )
    second = record_assistant_external_ip_regional_migration_intent(
        dbsession,
        assistant=assistant,
        desired_pool_location="asia-east",
    )
    dbsession.flush()

    assert first is not None
    assert second is first
    assert (
        dbsession.query(AssistantExternalIPRegionalMigration)
        .filter_by(external_ip_id=external_ip.id)
        .count()
        == 1
    )
