"""DAO for ``PlanGroup`` — curated bundles of switchable templates.

Plan groups are the catalog scoping for the customer-facing self-serve
``POST /v0/billing/plan`` endpoint. An account is on at most one group
(``BillingAccount.plan_group_id``) and may switch itself to any active
member template. Templates outside the account's group are still
assignable by admin via ``BillingPlanAssignmentDAO.set_plan`` directly,
the group only gates the *self-serve* path.

This module is intentionally small: groups are lightweight admin
configuration objects, not part of any billing or invoicing hot path.
The interesting policy logic (downgrade detection, AT_BOUNDARY
deferral) lives where it can be reused by both this DAO's helpers and
the admin override endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from orchestra.db.models.orchestra_models import (
    BillingAccount,
    BillingPlanTemplate,
    PlanGroup,
    PlanGroupMember,
)


class PlanGroupNotFoundError(Exception):
    """Raised when a group / member operation references an unknown id."""


class PlanGroupMemberError(Exception):
    """Raised on invalid membership operations (duplicate, unknown template, etc.)."""


class PlanGroupInUseError(Exception):
    """Raised when an operator tries to deprecate a plan group while one
    or more billing accounts are still pointing at it.

    Deprecating a group while accounts still reference it is mostly a
    UX-level bug — the customer's Switch Plan section silently empties
    out (deprecated groups are filtered server-side) without any
    operator intent, and admin tooling has no way to tell after the
    fact whether the operator meant to move the account or just
    forgot to reassign. ``BillingAccount.plan_group_id`` is ``NOT NULL``
    — every account must be on *some* group at all times — so the only
    valid resolution is to reassign every referencing account to a
    different group (typically the default group, id=1) before
    deprecating this one.
    """

    def __init__(self, group_id: int, account_count: int) -> None:
        self.group_id = group_id
        self.account_count = account_count
        super().__init__(
            f"Cannot deprecate plan_group id={group_id}: "
            f"{account_count} billing account(s) still point at it. "
            "Reassign every account to a different group "
            "(e.g. the default group id=1) before retrying — "
            "plan_group_id is NOT NULL and there is no opt-out state.",
        )


@dataclass(frozen=True)
class PlanGroupAvailableMember:
    """One row in the ``available-plans`` response for a customer.

    Holds just enough to render the switch UI: the template id (so the
    POST round-trip can echo it back), denormalised display fields
    (template names + commit summary), and a ``position`` (the group
    rung) that drives downgrade detection. ``is_current`` flags the
    member that matches the account's currently-active assignment so
    the UI can render a "you are here" badge.
    """

    template_id: int
    template_name: str
    template_display_name: str
    billing_mode: str
    commit_amount: Optional[float]
    currency: str
    commit_period: Optional[str]
    commit_schedule: Optional[str]
    position: Optional[int]
    is_current: bool
    is_active: bool


class BillingPlanGroupDAO:
    """CRUD + membership management for plan groups.

    Encapsulates the small set of invariants worth keeping out of the
    view layer:

    * group ``name`` is unique catalog-wide (DB index);
    * a (group, template) pair appears at most once (PK);
    * ``position`` is unique within a group when set (partial unique
      index in the migration).

    The DAO does not enforce "currently-active template must be a
    member of the group" — that is intentionally a soft invariant
    that the customer-facing ``/billing/available-plans`` endpoint
    handles by hiding the switcher entirely (see the FE hide-rule:
    no ``is_current`` member ⇒ empty list ⇒ section unrendered).
    Admins can therefore move accounts onto bespoke templates
    without first having to mint a new group, and accounts on the
    auto-applied default group with a custom contract just
    silently get no switcher UI.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Group CRUD
    # ------------------------------------------------------------------
    def get_by_id(self, group_id: int) -> Optional[PlanGroup]:
        return self.session.get(PlanGroup, group_id)

    def get_by_name(self, name: str) -> Optional[PlanGroup]:
        return (
            self.session.execute(select(PlanGroup).where(PlanGroup.name == name))
            .scalars()
            .first()
        )

    def list_all(self, *, include_inactive: bool = False) -> List[PlanGroup]:
        stmt = select(PlanGroup)
        if not include_inactive:
            stmt = stmt.where(PlanGroup.is_active.is_(True))
        return list(
            self.session.execute(stmt.order_by(PlanGroup.name.asc())).scalars().all(),
        )

    def create_group(
        self,
        *,
        name: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        is_active: bool = True,
        created_by_user_id: Optional[str] = None,
    ) -> PlanGroup:
        if not name or not name.strip():
            raise ValueError("PlanGroup.name must be non-empty")
        group = PlanGroup(
            name=name.strip(),
            display_name=display_name,
            description=description,
            is_active=is_active,
            created_by_user_id=created_by_user_id,
        )
        self.session.add(group)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise PlanGroupMemberError(
                f"PlanGroup with name={name!r} already exists.",
            ) from exc
        return group

    def count_assigned_accounts(self, group_id: int) -> int:
        """Count billing accounts currently pointing at ``group_id``."""
        return int(
            self.session.execute(
                select(func.count())
                .select_from(BillingAccount)
                .where(BillingAccount.plan_group_id == group_id),
            ).scalar_one(),
        )

    def update_group(
        self,
        group_id: int,
        *,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> PlanGroup:
        group = self.get_by_id(group_id)
        if group is None:
            raise PlanGroupNotFoundError(f"Unknown plan_group id={group_id}")
        # Deprecation guard: refuse to deactivate a group that any
        # account still references. We could SET-NULL them on the spot
        # but that silently opts customers out of self-serve switching
        # — better to surface as an explicit operator decision.
        if is_active is False and group.is_active is True:
            assigned = self.count_assigned_accounts(group_id)
            if assigned > 0:
                raise PlanGroupInUseError(group_id, assigned)
        if display_name is not None:
            group.display_name = display_name or None
        if description is not None:
            group.description = description or None
        if is_active is not None:
            group.is_active = is_active
        self.session.flush()
        return group

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------
    def list_members(
        self,
        group_id: int,
        *,
        include_inactive_templates: bool = True,
    ) -> List[PlanGroupMember]:
        """Return members ordered by (position ASC NULLS LAST, added_at ASC).

        ``include_inactive_templates=True`` (default) returns *all*
        members, including those whose template has been deprecated
        (``is_active=false``) — this is what the admin UI wants so
        operators can see drift. The customer-facing
        ``list_available_for_account`` always filters out inactive
        templates because deprecated plans must not be assignable.
        """
        stmt = (
            select(PlanGroupMember)
            .options(joinedload(PlanGroupMember.template))
            .where(PlanGroupMember.group_id == group_id)
            .order_by(
                PlanGroupMember.position.asc().nulls_last(),
                PlanGroupMember.added_at.asc(),
            )
        )
        members = list(self.session.execute(stmt).scalars().all())
        if include_inactive_templates:
            return members
        return [m for m in members if m.template.is_active]

    def add_member(
        self,
        *,
        group_id: int,
        template_id: int,
        position: Optional[int] = None,
    ) -> PlanGroupMember:
        if self.get_by_id(group_id) is None:
            raise PlanGroupNotFoundError(f"Unknown plan_group id={group_id}")
        if self.session.get(BillingPlanTemplate, template_id) is None:
            raise PlanGroupMemberError(
                f"Unknown billing_plan_template id={template_id}",
            )
        existing = self.session.execute(
            select(PlanGroupMember).where(
                PlanGroupMember.group_id == group_id,
                PlanGroupMember.template_id == template_id,
            ),
        ).scalar_one_or_none()
        if existing is not None:
            raise PlanGroupMemberError(
                f"Template id={template_id} is already a member of "
                f"group id={group_id}.",
            )
        if position is not None and position < 0:
            raise PlanGroupMemberError(
                "PlanGroupMember.position must be >= 0 when set",
            )
        member = PlanGroupMember(
            group_id=group_id,
            template_id=template_id,
            position=position,
        )
        self.session.add(member)
        try:
            self.session.flush()
        except IntegrityError as exc:
            # The partial unique index fires when ``position`` collides
            # with another member already at that rung. Convert to a
            # readable error so the admin UI can surface it as a 409.
            raise PlanGroupMemberError(
                f"Position {position} is already used by another member "
                f"of group id={group_id}.",
            ) from exc
        return member

    def remove_member(self, *, group_id: int, template_id: int) -> None:
        result = self.session.execute(
            delete(PlanGroupMember).where(
                PlanGroupMember.group_id == group_id,
                PlanGroupMember.template_id == template_id,
            ),
        )
        if result.rowcount == 0:
            raise PlanGroupMemberError(
                f"Template id={template_id} is not a member of group "
                f"id={group_id}.",
            )

    def set_positions(
        self,
        *,
        group_id: int,
        positions: List[tuple[int, Optional[int]]],
    ) -> List[PlanGroupMember]:
        """Atomically rewrite the position column for a set of members.

        ``positions`` is a list of ``(template_id, position)`` tuples
        covering every member whose rung you want to change. Members
        not listed keep their existing position. The whole rewrite
        happens inside one flush so the partial unique index never
        sees an intermediate collision (we clear all targeted rows to
        NULL first, then re-set in a second pass).
        """
        if self.get_by_id(group_id) is None:
            raise PlanGroupNotFoundError(f"Unknown plan_group id={group_id}")
        member_map = {
            m.template_id: m
            for m in self.list_members(group_id, include_inactive_templates=True)
        }
        for template_id, _ in positions:
            if template_id not in member_map:
                raise PlanGroupMemberError(
                    f"Template id={template_id} is not a member of group "
                    f"id={group_id}; add it first.",
                )
        # Two-pass clear-then-set to avoid intermediate unique
        # collisions. The partial index only enforces uniqueness for
        # NON-NULL positions, so blanking is always safe.
        for template_id, _ in positions:
            member_map[template_id].position = None
        self.session.flush()
        for template_id, position in positions:
            if position is not None and position < 0:
                raise PlanGroupMemberError(
                    "PlanGroupMember.position must be >= 0 when set",
                )
            member_map[template_id].position = position
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise PlanGroupMemberError(
                "Duplicate positions in re-ordering — every populated "
                "position must be unique within the group.",
            ) from exc
        return [member_map[tid] for tid, _ in positions]

    # ------------------------------------------------------------------
    # Customer-facing helpers
    # ------------------------------------------------------------------
    def list_available_for_account(
        self,
        billing_account: BillingAccount,
    ) -> List[PlanGroupAvailableMember]:
        """Render the switchable plan list for a customer billing page.

        Returns ``[]`` when the assigned group has no active members.
        Always filters out members whose template is inactive —
        deprecated plans must never be self-assigned.

        ``is_current`` is set on the member matching the account's
        currently-active template so the UI can render a "current"
        badge. Note this is a *template* match, not an *assignment*
        match: an account whose active template isn't a member of
        the group will see no rung flagged ``is_current`` — the
        view layer keys the switcher's hide-rule off that fact, so
        this is an expected state (e.g. enterprise customers parked
        on the platform-default group), not drift.

        ``plan_group_id`` is NOT NULL by schema invariant; the
        ``is None`` guard below is belt-and-braces against a
        corruption (caught & flagged separately by the
        ``plan_group_null_pointer`` reconciliation check).
        """
        if billing_account.plan_group_id is None:
            return []
        members = self.list_members(
            billing_account.plan_group_id,
            include_inactive_templates=False,
        )
        if not members:
            return []
        # Resolve the account's current template id without spinning
        # up the BillingPlanAssignmentDAO (would create a circular
        # import). The pointer is denormalised onto the BA, and we
        # only need the template id, so a small targeted query is
        # cheaper than a full ``EffectivePlan`` resolve.
        from orchestra.db.models.orchestra_models import BillingPlanAssignment

        current_template_id: Optional[int] = None
        if billing_account.plan_assignment_id is not None:
            current_template_id = self.session.execute(
                select(BillingPlanAssignment.template_id).where(
                    BillingPlanAssignment.id == billing_account.plan_assignment_id,
                ),
            ).scalar_one_or_none()
        out: List[PlanGroupAvailableMember] = []
        for member in members:
            t = member.template
            out.append(
                PlanGroupAvailableMember(
                    template_id=t.id,
                    template_name=t.name,
                    template_display_name=t.display_name or t.name,
                    billing_mode=t.billing_mode,
                    commit_amount=(
                        float(t.commit_amount) if t.commit_amount is not None else None
                    ),
                    currency=t.currency,
                    commit_period=t.commit_period,
                    commit_schedule=t.commit_schedule,
                    position=member.position,
                    is_current=(t.id == current_template_id),
                    is_active=bool(t.is_active),
                ),
            )
        return out

    def is_member(self, *, group_id: int, template_id: int) -> bool:
        """True iff ``template_id`` is currently a member of ``group_id``."""
        row = self.session.execute(
            select(PlanGroupMember.template_id).where(
                PlanGroupMember.group_id == group_id,
                PlanGroupMember.template_id == template_id,
            ),
        ).scalar_one_or_none()
        return row is not None

    def get_member_position(
        self,
        *,
        group_id: int,
        template_id: int,
    ) -> Optional[int]:
        """Return the member's ``position`` (NULL = unordered) or raise if absent."""
        row = self.session.execute(
            select(PlanGroupMember.position).where(
                PlanGroupMember.group_id == group_id,
                PlanGroupMember.template_id == template_id,
            ),
        ).one_or_none()
        if row is None:
            raise PlanGroupMemberError(
                f"Template id={template_id} is not a member of group "
                f"id={group_id}.",
            )
        return row[0]

    def template_group_count(self, template_id: int) -> int:
        """How many groups list this template? Drives the admin "Used in N groups" badge."""
        return int(
            self.session.execute(
                select(func.count())
                .select_from(PlanGroupMember)
                .where(PlanGroupMember.template_id == template_id),
            ).scalar_one(),
        )


# ---------------------------------------------------------------------------
# Pure helpers — downgrade detection
#
# Lives module-level so both the customer endpoint and the admin
# override endpoint share one definition.  The current policy is:
# a switch is a downgrade iff BOTH the current template AND the target
# template have a populated position in the group AND target.position
# < current.position. Unordered groups (NULL positions) never count as
# downgrades — every move is a side-grade.
# ---------------------------------------------------------------------------


def is_downgrade_within_group(
    *,
    current_position: Optional[int],
    target_position: Optional[int],
) -> bool:
    """Return True iff the move from ``current`` to ``target`` is a downgrade.

    Both positions must be populated for the comparison to be defined;
    if either side is NULL (unordered group, or current template lives
    outside the group) the move is treated as a side-grade.
    """
    if current_position is None or target_position is None:
        return False
    return target_position < current_position
