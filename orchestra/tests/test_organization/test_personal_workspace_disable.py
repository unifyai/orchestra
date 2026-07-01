from __future__ import annotations

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import AssistantCleanupTask, AssistantContact
from orchestra.services.assistant_cleanup_service import CleanupSource
from orchestra.services.personal_workspace_service import (
    disable_personal_workspace_for_org_member,
)
from orchestra.tests.test_billing.conftest import (
    make_assistant,
    make_billing_account,
    make_contact,
    make_org,
    make_user,
)


def test_disable_personal_workspace_soft_deletes_contacts_and_queues_cleanup(
    dbsession: Session,
):
    user_ba = make_billing_account(dbsession, credits=0)
    user = make_user(dbsession, "disable_personal_u1", user_ba)
    org_ba = make_billing_account(dbsession, credits=100)
    org = make_org(dbsession, user, org_ba, name="Disable Personal Org")
    personal = make_assistant(dbsession, user.id, first_name="Personal")
    contact = make_contact(
        dbsession,
        personal.agent_id,
        contact_type="phone",
        contact_value="+15554560001",
        provider="twilio",
        country_code="US",
    )
    dbsession.flush()

    result = disable_personal_workspace_for_org_member(
        dbsession,
        user.id,
        org.id,
    )
    dbsession.flush()

    dbsession.refresh(user)
    dbsession.refresh(contact)

    assert result.disabled is True
    assert result.personal_assistants == 1
    assert result.contacts_soft_deleted == 1
    assert result.cleanup_tasks_queued == 1
    assert user.personal_workspace_disabled_at is not None
    assert user.personal_workspace_disabled_reason == "organization_membership"
    assert user.personal_workspace_disabled_org_id == org.id
    assert contact.status == "deleted"
    assert contact.deleted_at is not None

    task = (
        dbsession.query(AssistantCleanupTask)
        .filter(AssistantCleanupTask.assistant_id == personal.agent_id)
        .one()
    )
    assert task.source_flow == CleanupSource.PERSONAL_WORKSPACE_DISABLED.value

    active_contacts = (
        dbsession.query(AssistantContact)
        .filter(
            AssistantContact.assistant_id == personal.agent_id,
            AssistantContact.status != "deleted",
        )
        .all()
    )
    assert active_contacts == []
