"""Staff access: Unify people sitting in a customer organization.

White-glove onboarding puts a Unify person inside the customer's org to
provision it, wire up integrations and hand over. They are not the
customer's own staff, and the customer should be able to see that at a
glance and see when the arrangement ends.

Two fields on ``OrganizationMember`` carry this:

* ``is_staff_access`` — the marker, surfaced to the customer as a badge.
* ``staff_access_expires_at`` — when it lapses; ``NULL`` never lapses.

Grants are bounded by default (``settings.staff_access_default_days``).
An unbounded grant is a deliberate override for standing arrangements —
sales partnerships, co-selling — rather than something a grant can drift
into by nobody revisiting it.

Expiry is enforced in
:meth:`ResourceAccessDAO.check_org_member_permission`, not by deleting
memberships: a lapsed grant stops authorising the instant it passes,
without a scheduled job having to be correct or on time, and extending
the grant restores access. :func:`lapsed_staff_grants` exists to *report*
lapsed seats so a human can remove them properly through the ordinary
member-removal path, which also deprovisions assistants and API keys.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Organization, OrganizationMember
from orchestra.services.personal_workspace_service import (
    UNIFY_ORGANIZATION_NAME,
    user_is_unify_member,
)
from orchestra.settings import settings


def staff_access_has_lapsed(
    member: OrganizationMember,
    now: Optional[datetime] = None,
) -> bool:
    """Whether *member*'s staff grant has expired.

    A grant with no expiry never lapses. Callers should check
    ``member.is_staff_access`` first — this answers only the timing.
    """
    if member.staff_access_expires_at is None:
        return False
    expires_at = member.staff_access_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= (now or datetime.now(timezone.utc))


def default_staff_access_expiry(now: Optional[datetime] = None) -> datetime:
    """The bounded window a new staff grant gets unless overridden."""
    return (now or datetime.now(timezone.utc)) + timedelta(
        days=settings.staff_access_default_days,
    )


def is_staff_in_organization(
    session: Session,
    user_id: str,
    organization_id: int,
) -> bool:
    """Whether *user_id* joining *organization_id* is Unify staff.

    True for a member of the Unify organization joining any *other* org.
    Unify's own org is excluded: its members are its actual staff, not
    guests in someone else's tenant, and badging them there would be
    noise.
    """
    org = session.get(Organization, organization_id)
    if org is None or org.name == UNIFY_ORGANIZATION_NAME:
        return False
    return user_is_unify_member(session, user_id)


def apply_staff_access_on_join(
    session: Session,
    member: OrganizationMember,
) -> bool:
    """Mark *member* as staff access if they are Unify staff in a customer org.

    The organization owner is never marked. Ownership is the opposite of
    a temporary assist, and a badge (or an expiry) on the owner's own seat
    would be actively misleading. During white-glove the Unify provisioner
    *is* the owner until hand-over; they pick the marker up when ownership
    transfers away and they are demoted.

    Returns True when the marker was applied.
    """
    org = session.get(Organization, member.organization_id)
    if org is not None and org.owner_id == member.user_id:
        return False
    if not is_staff_in_organization(session, member.user_id, member.organization_id):
        return False

    member.is_staff_access = True
    member.staff_access_expires_at = default_staff_access_expiry()
    return True


def set_staff_access(
    member: OrganizationMember,
    *,
    staff_access: bool,
    expires_in_days: Optional[int],
) -> None:
    """Set or clear *member*'s staff grant.

    ``expires_in_days=None`` with ``staff_access=True`` makes the grant
    unbounded — the partner-engagement override. Clearing the marker also
    clears the expiry so a later re-grant starts from the default window
    rather than inheriting a stale one.
    """
    if not staff_access:
        member.is_staff_access = False
        member.staff_access_expires_at = None
        return

    member.is_staff_access = True
    if expires_in_days is None:
        member.staff_access_expires_at = None
    else:
        member.staff_access_expires_at = datetime.now(timezone.utc) + timedelta(
            days=expires_in_days,
        )


def lapsed_staff_grants(session: Session) -> list[OrganizationMember]:
    """Staff seats whose grant has expired and are still members.

    Reporting only — these already authorise nothing. Surfacing them lets
    a human remove the seat through the ordinary removal path, which also
    deprovisions assistants and revokes org-scoped API keys.
    """
    rows = session.execute(
        select(OrganizationMember).where(
            OrganizationMember.is_staff_access.is_(True),
            OrganizationMember.staff_access_expires_at.isnot(None),
            OrganizationMember.staff_access_expires_at <= datetime.now(timezone.utc),
        ),
    ).scalars()
    return list(rows)
