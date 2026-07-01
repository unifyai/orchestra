from __future__ import annotations

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    AssistantCleanupTask,
    AssistantContact,
    OrganizationMember,
)
from orchestra.services.assistant_cleanup_service import CleanupSource
from orchestra.services.personal_workspace_service import (
    disable_personal_workspace_for_org_member,
    reenable_personal_workspace_if_no_org,
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


def test_reenable_personal_workspace_after_leaving_last_org(dbsession: Session):
    """Leaving the last (non-Unify) org clears the disabled flag so personal
    billing works again."""
    user_ba = make_billing_account(dbsession, credits=0)
    user = make_user(dbsession, "reenable_personal_u1", user_ba)
    org_ba = make_billing_account(dbsession, credits=100)
    org = make_org(dbsession, user, org_ba, name="Reenable Personal Org")
    dbsession.flush()

    disable_personal_workspace_for_org_member(dbsession, user.id, org.id)
    dbsession.flush()
    dbsession.refresh(user)
    assert user.personal_workspace_disabled_at is not None

    # Simulate leaving/org deletion: drop the membership, then re-enable.
    dbsession.query(OrganizationMember).filter(
        OrganizationMember.organization_id == org.id,
        OrganizationMember.user_id == user.id,
    ).delete()
    dbsession.flush()

    reenabled = reenable_personal_workspace_if_no_org(dbsession, user.id)
    dbsession.flush()
    dbsession.refresh(user)

    assert reenabled is True
    assert user.personal_workspace_disabled_at is None
    assert user.personal_workspace_disabled_reason is None
    assert user.personal_workspace_disabled_org_id is None


def test_reenable_is_noop_while_still_in_another_org(dbsession: Session):
    """A user still in another org keeps the disabled flag."""
    user_ba = make_billing_account(dbsession, credits=0)
    user = make_user(dbsession, "reenable_personal_u2", user_ba)
    org_a_ba = make_billing_account(dbsession, credits=100)
    org_a = make_org(dbsession, user, org_a_ba, name="Reenable Org A")
    org_b_ba = make_billing_account(dbsession, credits=100)
    org_b = make_org(dbsession, user, org_b_ba, name="Reenable Org B")
    dbsession.flush()

    disable_personal_workspace_for_org_member(dbsession, user.id, org_a.id)
    dbsession.flush()

    # Leave only org A; still a member of org B.
    dbsession.query(OrganizationMember).filter(
        OrganizationMember.organization_id == org_a.id,
        OrganizationMember.user_id == user.id,
    ).delete()
    dbsession.flush()

    reenabled = reenable_personal_workspace_if_no_org(dbsession, user.id)
    dbsession.refresh(user)

    assert reenabled is False
    assert user.personal_workspace_disabled_at is not None
