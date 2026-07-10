from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import available_timezones

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    AssistantSecret,
    User,
)


@dataclass
class AssistantSpendingCapResult:
    """Result of setting an assistant spending cap."""

    monthly_spending_cap: Optional[float] = None
    effective_limit: Optional[float] = None
    parent_limit: Optional[float] = None


VALID_TIMEZONES = available_timezones()


def _active_contact_assistant_ids_subquery(contact_type: str, contact_value: str):
    """Subquery of assistant IDs with an active contact of ``contact_type``/``value``."""
    return select(AssistantContact.assistant_id).where(
        AssistantContact.contact_type == contact_type,
        AssistantContact.contact_value == contact_value,
        AssistantContact.status != "deleted",
    )


def _apply_assistant_contact_value_filters(
    stmt,
    *,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    assistant_whatsapp_number: Optional[str] = None,
):
    """Restrict ``stmt`` to assistants matching active ``assistant_contacts`` rows."""
    if phone is not None:
        stmt = stmt.where(
            Assistant.agent_id.in_(
                _active_contact_assistant_ids_subquery("phone", phone),
            ),
        )
    if email is not None:
        stmt = stmt.where(
            Assistant.agent_id.in_(
                _active_contact_assistant_ids_subquery("email", email),
            ),
        )
    if assistant_whatsapp_number is not None:
        stmt = stmt.where(
            Assistant.agent_id.in_(
                _active_contact_assistant_ids_subquery(
                    "whatsapp",
                    assistant_whatsapp_number,
                ),
            ),
        )
    return stmt


def _require_assistant_scope(
    organization_id: Optional[int],
    user_id: Optional[str],
) -> None:
    """Enforce XOR on owner-scoped assistant lookups.

    Used by routing-oriented APIs (``coordinator``, ``resolve_token``)
    that operate on either an organization or a personal user but never
    both at once. Mirrors the polymorphic owner contract that
    :class:`~orchestra.db.models.orchestra_models.SlackInstall` carries.
    """
    has_org = organization_id is not None
    has_user = user_id is not None
    if has_org == has_user:
        raise ValueError(
            "Provide exactly one of organization_id or user_id "
            f"(got organization_id={organization_id!r}, user_id={user_id!r}).",
        )


class AssistantDAO:
    """
    Data access object for Assistant operations.

    Supports both personal assistants (organization_id is NULL) and
    organizational assistants (organization_id is set).

    Organizational assistants use a creator-owned lifecycle model:
    ``user_id`` remains the creating user for lineage, cleanup, and
    creator-scoped listings, while ``organization_id`` defines the
    collaborative org scope and RBAC context.
    """

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _normalize_name_part(value: Optional[str]) -> Optional[str]:
        """Normalize optional name text for case-insensitive matching."""
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None

    def create_assistant(
        self,
        user_id: str,
        first_name: Optional[str],
        surname: Optional[str],
        age: Optional[int],
        nationality: Optional[str],
        about: Optional[str],
        weekly_limit: Optional[Decimal],
        max_parallel: Optional[int],
        profile_photo: Optional[str] = None,
        profile_video: Optional[str] = None,
        desktop_mode: Optional[str] = None,
        voice_id: Optional[str] = None,
        voice_provider: Optional[str] = None,
        default_model: Optional[str] = None,
        default_reasoning_effort: Optional[str] = None,
        timezone: Optional[str] = None,
        organization_id: Optional[int] = None,
        owner_team_id: Optional[int] = None,
        is_local: bool = False,
        is_coordinator: bool = False,
        job_title: Optional[str] = None,
    ) -> Assistant:
        """
        Create a new Assistant.

        If organization_id is provided, creates an organizational assistant.
        If organization_id is None, creates a personal assistant.

        :param user_id: Personal assistants: owner. Org assistants:
            creator/lifecycle owner retained on the row.
        :param organization_id: Optional organization scope for org assistants.
            None means a personal assistant.
        :param owner_team_id: When set, the team is the product-level owner:
            the assistant lives entirely in the team's shared root and
            ``user_id`` records only the hiring member.
        :return: The created Assistant.
        """

        if timezone is not None and timezone not in VALID_TIMEZONES:
            raise ValueError(f"'{timezone}' is not a valid IANA timezone.")

        assistant = Assistant(
            user_id=user_id,
            organization_id=organization_id,
            owner_team_id=owner_team_id,
            first_name=first_name,
            surname=surname,
            job_title=job_title,
            age=age,
            nationality=nationality,
            profile_photo=profile_photo,
            profile_video=profile_video,
            desktop_mode=desktop_mode,
            about=about,
            weekly_limit=weekly_limit,
            max_parallel=max_parallel,
            voice_id=voice_id,
            voice_provider=voice_provider,
            default_model=default_model,
            default_reasoning_effort=default_reasoning_effort,
            timezone=timezone,
            is_local=is_local,
            is_coordinator=is_coordinator,
        )
        self.session.add(assistant)
        self.session.flush()
        return assistant

    def find_by_natural_key(
        self,
        *,
        user_id: str,
        organization_id: Optional[int],
        first_name: Optional[str],
        surname: Optional[str],
    ) -> Optional[Assistant]:
        """Return an assistant with the same normalized natural-name key.

        Organization scope:
            ``organization_id + first_name + surname``

        Personal scope:
            ``user_id + first_name + surname`` with ``organization_id`` NULL
        """
        normalized_first_name = self._normalize_name_part(first_name)
        if normalized_first_name is None:
            return None
        normalized_surname = self._normalize_name_part(surname) or ""

        stmt = select(Assistant).where(
            func.lower(func.trim(func.coalesce(Assistant.first_name, "")))
            == normalized_first_name,
            func.lower(func.trim(func.coalesce(Assistant.surname, "")))
            == normalized_surname,
        )
        if organization_id is None:
            stmt = stmt.where(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )
        else:
            stmt = stmt.where(Assistant.organization_id == organization_id)

        rows = self.session.execute(
            stmt.order_by(Assistant.created_at.asc(), Assistant.agent_id.asc()),
        ).scalars()
        return rows.first()

    def get_assistant_by_id(
        self,
        user_id: str,
        agent_id: int,
        organization_id: Optional[int] = None,
    ) -> Optional[Assistant]:
        """
        Retrieve an Assistant by agent ID.

        For personal assistants (organization_id=None):
            Returns assistant if user_id matches and organization_id is NULL.

        For org assistants (organization_id is set):
            Returns assistant if organization_id matches.
            The user_id check is skipped because ``user_id`` remains creator
            metadata for org assistants, while access is governed by org scope
            and permission checks.

        :param user_id: User ID (used for personal assistant lookup).
        :param agent_id: Assistant agent ID.
        :param organization_id: Organization ID for org context (None = personal).
        :return: Assistant if found, None otherwise.
        """
        if organization_id is not None:
            # Org context: find by agent_id and organization_id
            stmt = select(Assistant).where(
                Assistant.agent_id == agent_id,
                Assistant.organization_id == organization_id,
            )
        else:
            # Personal context: find by agent_id, user_id, and organization_id is NULL
            stmt = select(Assistant).where(
                Assistant.agent_id == agent_id,
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )
        result = self.session.execute(stmt).scalar_one_or_none()
        return result

    def get_assistant_by_agent_id(self, agent_id: int) -> Optional[Assistant]:
        """
        Retrieve an Assistant by agent ID only (no user/org context).

        Used for internal operations like transfers where we need to fetch
        the assistant regardless of current API key context.

        :param agent_id: Assistant agent ID.
        :return: Assistant if found, None otherwise.
        """
        stmt = select(Assistant).where(Assistant.agent_id == agent_id)
        result = self.session.execute(stmt).scalar_one_or_none()
        return result

    def coordinator(
        self,
        *,
        organization_id: Optional[int] = None,
        user_id: Optional[str] = None,
    ) -> Optional[Assistant]:
        """Return a Coordinator assistant, scoped by owner.

        The Coordinator handles cross-assistant administrative traffic
        (Slack DMs to unknown contacts, ambiguous ``@app <token>``
        mentions, org-wide announcements). A Coordinator is personal to a
        user even inside an organization: an org holds one workspace
        Coordinator *per member*, so the only unambiguous lookup is by a
        specific ``(user_id, organization_id)`` membership.

        At least one of ``organization_id`` or ``user_id`` must be
        supplied:

        * ``user_id`` + ``organization_id`` — that member's workspace
          Coordinator. Unique per
          ``ux_assistants_one_workspace_coordinator_per_membership``.
        * ``user_id`` only — the user's personal Coordinator. Unique per
          ``ux_assistants_one_personal_coordinator_per_user`` (assistants
          with ``organization_id IS NULL``).
        * ``organization_id`` only — a Coordinator within the org. Since
          an org can hold one per member, this returns the lowest
          ``agent_id`` match deterministically rather than assuming a
          single row.
        """
        if organization_id is None and user_id is None:
            raise ValueError(
                "Provide organization_id, user_id, or both "
                f"(got organization_id={organization_id!r}, user_id={user_id!r}).",
            )
        stmt = select(Assistant).where(Assistant.is_coordinator.is_(True))
        if user_id is not None and organization_id is not None:
            stmt = stmt.where(
                Assistant.user_id == user_id,
                Assistant.organization_id == organization_id,
            )
            return self.session.execute(stmt).scalar_one_or_none()
        if user_id is not None:
            stmt = stmt.where(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )
            return self.session.execute(stmt).scalar_one_or_none()
        stmt = stmt.where(Assistant.organization_id == organization_id).order_by(
            Assistant.agent_id.asc(),
        )
        return self.session.execute(stmt).scalars().first()

    def resolve_token(
        self,
        token: str,
        *,
        organization_id: Optional[int] = None,
        user_id: Optional[str] = None,
    ) -> list[Assistant]:
        """Resolve a Slack ``@<app> <token>`` addressing token to assistants.

        A token matches an assistant by any of three forms (all
        case-insensitive, owner-scoped):

        * **agent id** — when ``token`` is all digits, the numeric
          ``agent_id``. Globally unique, so it is the guaranteed
          disambiguator.
        * **first name** — ``first_name`` on its own (convenient when
          unique within the owner scope).
        * **full name** — ``"first surname"`` (trimmed), the canonical
          dedup form when two assistants share a first name.

        Exactly one of ``organization_id`` or ``user_id`` must be
        supplied:

        * ``organization_id`` — search the org's assistants.
        * ``user_id`` — search the user's personal assistants
          (``organization_id IS NULL``).

        An id match stays owner-scoped: an id belonging to a different
        organization or user does not resolve, preserving cross-scope
        isolation.

        The caller decides the next step based on the result-list length:

        * 0 → unknown token, route to coordinator with a hint.
        * 1 → unambiguous, route to that assistant.
        * >1 → ambiguous, route to coordinator with a disambiguation hint.
        """
        _require_assistant_scope(organization_id, user_id)
        token = (token or "").strip()
        if not token:
            return []
        normalized = token.lower()
        full_name = sa.func.trim(
            sa.func.concat(
                Assistant.first_name,
                " ",
                sa.func.coalesce(Assistant.surname, ""),
            ),
        )
        match_conditions = [
            sa.func.lower(Assistant.first_name) == normalized,
            sa.func.lower(full_name) == normalized,
        ]
        if token.isdigit():
            match_conditions.append(Assistant.agent_id == int(token))
        stmt = select(Assistant).where(or_(*match_conditions))
        if organization_id is not None:
            stmt = stmt.where(Assistant.organization_id == organization_id)
        else:
            stmt = stmt.where(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )
        stmt = stmt.order_by(Assistant.agent_id.asc())
        return list(self.session.execute(stmt).scalars().all())

    def list_assistants_for_user(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
        agent_id: Optional[int] = None,
        include_demo: bool = False,
        demo_only: bool = False,
    ) -> List[Assistant]:
        """
        List assistants accessible to a user based on API key context.

        For personal API key (organization_id=None):
            Returns only personal assistants (organization_id IS NULL)
            where user_id matches.

        For org API key (organization_id is set):
            Returns only assistants in that org where user_id matches
            (creator-owned listing semantics for org assistants).

        :param user_id: User ID.
        :param organization_id: Organization ID from API key context (None = personal).
        :param include_demo: If True, include demo assistants in results.
        :param demo_only: If True, only return demo assistants.
        :return: List of assistants.
        """
        if organization_id is not None:
            # Org context: user's assistants in this org
            stmt = select(Assistant).where(
                Assistant.user_id == user_id,
                Assistant.organization_id == organization_id,
            )
        else:
            # Personal context: only personal assistants
            stmt = select(Assistant).where(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )

        # Demo filtering
        if demo_only:
            stmt = stmt.where(Assistant.demo_id.isnot(None))
        elif not include_demo:
            stmt = stmt.where(Assistant.demo_id.is_(None))

        if (
            phone is not None
            or email is not None
            or assistant_whatsapp_number is not None
        ):
            stmt = _apply_assistant_contact_value_filters(
                stmt,
                phone=phone,
                email=email,
                assistant_whatsapp_number=assistant_whatsapp_number,
            )
        if user_phone is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        User.id == Assistant.user_id,
                        User.phone_number == user_phone,
                    ),
                ),
            )
        if user_whatsapp_number is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        User.id == Assistant.user_id,
                        User.whatsapp_number == user_whatsapp_number,
                    ),
                ),
            )
        if agent_id is not None:
            stmt = stmt.where(Assistant.agent_id == agent_id)
        result = self.session.execute(stmt).scalars().all()
        return result

    def list_all_org_assistants(
        self,
        organization_id: int,
        requesting_user_id: Optional[str] = None,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
        agent_id: Optional[int] = None,
        include_demo: bool = False,
        demo_only: bool = False,
    ) -> List[Assistant]:
        """
        List ALL assistants in an organization (for list_all_org=True).

        This returns all assistants in the org, regardless of who created them.
        Should only be called after verifying the user has assistant:read permission.

        When ``requesting_user_id`` is provided, org-scoped Coordinator rows are
        visible only when owned by that user. Non-coordinator assistants remain
        org-visible for collaborative workflows.

        :param organization_id: Organization ID.
        :param requesting_user_id: Optional caller user_id for coordinator
            visibility filtering.
        :param include_demo: If True, include demo assistants in results.
        :param demo_only: If True, only return demo assistants.
        :return: List of all assistants in the organization.
        """
        stmt = select(Assistant).where(
            Assistant.organization_id == organization_id,
        )
        if requesting_user_id is not None:
            stmt = stmt.where(
                or_(
                    Assistant.is_coordinator.is_(False),
                    Assistant.user_id == requesting_user_id,
                ),
            )

        # Demo filtering
        if demo_only:
            stmt = stmt.where(Assistant.demo_id.isnot(None))
        elif not include_demo:
            stmt = stmt.where(Assistant.demo_id.is_(None))

        if (
            phone is not None
            or email is not None
            or assistant_whatsapp_number is not None
        ):
            stmt = _apply_assistant_contact_value_filters(
                stmt,
                phone=phone,
                email=email,
                assistant_whatsapp_number=assistant_whatsapp_number,
            )
        if user_phone is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        User.id == Assistant.user_id,
                        User.phone_number == user_phone,
                    ),
                ),
            )
        if user_whatsapp_number is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        User.id == Assistant.user_id,
                        User.whatsapp_number == user_whatsapp_number,
                    ),
                ),
            )
        if agent_id is not None:
            stmt = stmt.where(Assistant.agent_id == agent_id)
        result = self.session.execute(stmt).scalars().all()
        return result

    def delete_assistant(
        self,
        user_id: str,
        agent_id: int,
        organization_id: Optional[int] = None,
    ) -> None:
        """
        Delete an Assistant.

        For personal assistants: requires user_id match.
        For org assistants: requires organization_id match.
        Permission checks should be done at the API layer.

        :param user_id: User ID (for personal assistants).
        :param agent_id: Assistant agent ID.
        :param organization_id: Organization ID for org context (None = personal).
        """
        assistant = self.get_assistant_by_id(user_id, agent_id, organization_id)
        if assistant:
            self.session.delete(assistant)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

    def update_assistant(
        self,
        user_id: str,
        agent_id: int,
        update_data: Dict[str, Any],
        organization_id: Optional[int] = None,
    ) -> Optional[Assistant]:
        """
        Update configuration for an existing Assistant.

        :param user_id: User ID.
        :param agent_id: Assistant agent ID.
        :param update_data: Dictionary of fields to update.
        :param organization_id: Organization ID for org context (None = personal).
        :return: Updated assistant or None if not found.
        """
        assistant = self.get_assistant_by_id(user_id, agent_id, organization_id)
        if not assistant:
            return None

        if "timezone" in update_data:
            tz = update_data["timezone"]
            if tz is not None and tz not in VALID_TIMEZONES:
                raise ValueError(f"'{tz}' is not a valid IANA timezone.")

        # Handle monthly_spending_cap with validation via set_spending_cap
        if "monthly_spending_cap" in update_data:
            new_cap = update_data.pop("monthly_spending_cap")
            self.set_spending_cap(agent_id, user_id, new_cap)

        # Track changes for contact sync
        should_sync_timezone = False
        should_sync_bio = False
        should_sync_first_name = False
        should_sync_surname = False

        if "timezone" in update_data:
            old_timezone = assistant.timezone
            new_timezone = update_data["timezone"]
            if new_timezone != old_timezone:
                should_sync_timezone = True

        if "about" in update_data:
            old_about = assistant.about
            new_about = update_data["about"]
            if new_about != old_about:
                should_sync_bio = True

        if "first_name" in update_data:
            if update_data["first_name"] != assistant.first_name:
                should_sync_first_name = True

        if "surname" in update_data:
            if update_data["surname"] != assistant.surname:
                should_sync_surname = True

        for key, value in update_data.items():
            setattr(assistant, key, value)

        self.session.add(assistant)
        self.session.flush()

        # Sync changes to Contact logs in Assistants project
        needs_sync = any(
            [
                should_sync_timezone,
                should_sync_bio,
                should_sync_first_name,
                should_sync_surname,
            ],
        )
        if needs_sync:
            from orchestra.services.contact_sync_service import ContactSyncService

            sync_service = ContactSyncService(self.session)
            if should_sync_timezone:
                sync_service.sync_assistant_timezone(
                    user_id=user_id,
                    organization_id=organization_id,
                    agent_id=assistant.agent_id,
                    new_timezone=assistant.timezone,
                )
            if should_sync_bio:
                sync_service.sync_assistant_bio(
                    user_id=user_id,
                    organization_id=organization_id,
                    agent_id=assistant.agent_id,
                    new_bio=assistant.about,
                )
            if should_sync_first_name:
                sync_service.sync_assistant_first_name(
                    user_id=user_id,
                    organization_id=organization_id,
                    agent_id=assistant.agent_id,
                    new_first_name=assistant.first_name,
                )
            if should_sync_surname:
                sync_service.sync_assistant_surname(
                    user_id=user_id,
                    organization_id=organization_id,
                    agent_id=assistant.agent_id,
                    new_surname=assistant.surname,
                )

        return assistant

    def transfer_to_organization(
        self,
        agent_id: int,
        user_id: str,
        organization_id: int,
    ) -> Optional[Assistant]:
        """
        Transfer a personal assistant to an organization.

        :param agent_id: Assistant agent ID.
        :param user_id: Current owner's user ID.
        :param organization_id: Target organization ID.
        :return: Updated assistant or None if not found.
        """
        # Get the personal assistant
        assistant = self.get_assistant_by_id(user_id, agent_id, organization_id=None)
        if not assistant:
            return None

        # Transfer to organization
        assistant.organization_id = organization_id
        self.session.add(assistant)
        self.session.flush()
        return assistant

    def transfer_to_personal(
        self,
        agent_id: int,
        organization_id: int,
        new_owner_user_id: str,
    ) -> Optional[Assistant]:
        """
        Transfer an organizational assistant to personal workspace.

        :param agent_id: Assistant agent ID.
        :param organization_id: Current organization ID.
        :param new_owner_user_id: User ID who will own the personal assistant.
        :return: Updated assistant or None if not found.
        """
        # Get the org assistant (use any user_id since we're checking by org)
        stmt = select(Assistant).where(
            Assistant.agent_id == agent_id,
            Assistant.organization_id == organization_id,
        )
        assistant = self.session.execute(stmt).scalar_one_or_none()
        if not assistant:
            return None

        # Transfer to personal
        assistant.organization_id = None
        assistant.user_id = new_owner_user_id
        self.session.add(assistant)
        self.session.flush()
        return assistant

    def list_all_assistants(
        self,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
        email: Optional[str] = None,
        agent_id: Optional[int] = None,
        require_secret_names: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Assistant]:
        """
        List all Assistants across all users with optional filtering.

        Contact filters (phone, email, whatsapp) are resolved via the
        ``assistant_contacts`` table rather than the legacy columns on
        the ``assistants`` table.

        ``require_secret_names`` restricts results to assistants that have at
        least one stored secret whose name is in the list (e.g.
        ``["MICROSOFT_REFRESH_TOKEN"]`` for the token-refresh cron) so callers
        that only care about one provider do not pay to enumerate every
        assistant. ``limit``/``offset`` paginate over a stable ``agent_id``
        ordering.

        This is an admin-level function that returns all assistants.
        """
        stmt = select(Assistant)
        if require_secret_names:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantSecret.agent_id == Assistant.agent_id,
                        AssistantSecret.secret_name.in_(require_secret_names),
                    ),
                ),
            )
        if (
            phone is not None
            or email is not None
            or assistant_whatsapp_number is not None
        ):
            stmt = _apply_assistant_contact_value_filters(
                stmt,
                phone=phone,
                email=email,
                assistant_whatsapp_number=assistant_whatsapp_number,
            )
        if user_phone is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        User.id == Assistant.user_id,
                        User.phone_number == user_phone,
                    ),
                ),
            )
        if user_whatsapp_number is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        User.id == Assistant.user_id,
                        User.whatsapp_number == user_whatsapp_number,
                    ),
                ),
            )
        if agent_id is not None:
            stmt = stmt.where(Assistant.agent_id == agent_id)
        if limit is not None:
            # Stable ordering so offset pagination is deterministic.
            stmt = stmt.order_by(Assistant.agent_id).limit(limit).offset(offset)
        result = self.session.execute(stmt).scalars().all()
        return result

    def set_spending_cap(
        self,
        agent_id: int,
        user_id: str,
        monthly_spending_cap: Optional[float],
    ) -> AssistantSpendingCapResult:
        """
        Set assistant spending cap with context-aware parent limit validation.

        For personal assistants (org_id=NULL): validates against user's personal limit.
        For org assistants: validates against member limit and org limit.

        :param agent_id: Assistant agent ID.
        :param user_id: User ID of the owner.
        :param monthly_spending_cap: New spending cap (None = no limit).
        :return: Result with new cap and effective limit.
        :raises ValueError: If assistant limit exceeds parent limit.
        :raises HTTPException: If assistant not found.
        """
        from orchestra.db.dao.organization_dao import OrganizationDAO
        from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
        from orchestra.db.dao.user_dao import UserDAO

        assistant = self.get_assistant_by_agent_id(agent_id)
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # Verify management rights. Team-owned assistants are a team asset:
        # any org member with assistant:write may manage the cap, not just
        # the hiring member recorded in user_id.
        if assistant.user_id != user_id:
            allowed = False
            if (
                assistant.owner_team_id is not None
                and assistant.organization_id is not None
            ):
                from orchestra.db.dao.resource_access_dao import ResourceAccessDAO

                allowed = ResourceAccessDAO(self.session).check_org_member_permission(
                    user_id,
                    assistant.organization_id,
                    "assistant:write",
                )
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Assistant not found.",
                )

        new_limit = monthly_spending_cap
        parent_limit: Optional[float] = None

        if assistant.organization_id is not None:
            # Organizational assistant - validate against member limit and org limit
            org_dao = OrganizationDAO(self.session)
            org_member_dao = OrganizationMemberDAO(self.session)

            org = org_dao.get(assistant.organization_id)
            member = org_member_dao.get_member(user_id, assistant.organization_id)

            # Get applicable limits (member limit and org limit). Team-owned
            # assistants bill the organization, not any individual member, so
            # no member's personal cap bounds them — only the org cap.
            member_limit = (
                float(member.monthly_spending_cap)
                if assistant.owner_team_id is None
                and member
                and member.monthly_spending_cap is not None
                else None
            )
            org_limit = (
                float(org.monthly_spending_cap)
                if org and org.monthly_spending_cap is not None
                else None
            )

            # The effective parent limit is the most restrictive
            if member_limit is not None and org_limit is not None:
                parent_limit = min(member_limit, org_limit)
            elif member_limit is not None:
                parent_limit = member_limit
            elif org_limit is not None:
                parent_limit = org_limit

            # Validate against parent limit
            if new_limit is not None and parent_limit is not None:
                if new_limit > parent_limit:
                    if member_limit is not None and new_limit > member_limit:
                        raise ValueError(
                            f"Assistant limit cannot exceed member limit (${member_limit:.2f})",
                        )
                    else:
                        raise ValueError(
                            f"Assistant limit cannot exceed organization limit (${org_limit:.2f})",
                        )
        else:
            # Personal assistant - validate against user's personal limit
            user_dao = UserDAO(self.session)
            user_row = user_dao.get_by_id(user_id)
            if user_row:
                user = user_row[0]
                parent_limit = (
                    float(user.monthly_spending_cap)
                    if user.monthly_spending_cap is not None
                    else None
                )

            if new_limit is not None and parent_limit is not None:
                if new_limit > parent_limit:
                    raise ValueError(
                        f"Assistant limit cannot exceed user limit (${parent_limit:.2f})",
                    )

        # Update the assistant's spending limit
        old_limit = assistant.monthly_spending_cap
        new_limit = Decimal(str(new_limit)) if new_limit is not None else None
        assistant.monthly_spending_cap = new_limit

        # Track when the limit value changes (for notification deduplication)
        if old_limit != new_limit:
            from datetime import datetime, timezone

            assistant.monthly_spending_cap_set_at = datetime.now(timezone.utc)

        # Calculate effective limit
        effective_limit = new_limit
        if parent_limit is not None:
            if effective_limit is None:
                effective_limit = parent_limit
            else:
                effective_limit = min(effective_limit, parent_limit)

        return AssistantSpendingCapResult(
            monthly_spending_cap=new_limit,
            effective_limit=effective_limit,
            parent_limit=parent_limit,
        )

    def get_spending_cap(self, agent_id: int) -> Optional[float]:
        """
        Get assistant's monthly spending cap.

        :param agent_id: Assistant agent ID.
        :return: Monthly spending cap or None if not set or assistant not found.
        """
        assistant = self.get_assistant_by_agent_id(agent_id)
        if assistant and assistant.monthly_spending_cap is not None:
            return float(assistant.monthly_spending_cap)
        return None

    def get_cumulative_spend(self, agent_id: int, month: str) -> float:
        """
        Get assistant's cumulative spend for a given month.

        Queries the credit_transaction ledger for debits attributed to this
        assistant within the calendar month.

        :param agent_id: Assistant agent ID.
        :param month: Month in YYYY-MM format.
        :return: Cumulative spend for the month (0.0 if no spend data).
        """
        from datetime import datetime

        from orchestra.db.dao.credit_transaction_dao import CreditTransactionDAO
        from orchestra.db.models.orchestra_models import Organization, User

        assistant = self.get_assistant_by_agent_id(agent_id)
        if not assistant:
            return 0.0

        # Resolve billing account
        if assistant.organization_id:
            org = (
                self.session.query(Organization)
                .filter(Organization.id == assistant.organization_id)
                .first()
            )
            ba_id = org.billing_account_id if org else None
        else:
            user = self.session.query(User).filter(User.id == assistant.user_id).first()
            ba_id = user.billing_account_id if user else None

        if ba_id is None:
            return 0.0

        year, mon = map(int, month.split("-"))
        month_start = datetime(year, mon, 1)
        if mon == 12:
            month_end = datetime(year + 1, 1, 1)
        else:
            month_end = datetime(year, mon + 1, 1)

        dao = CreditTransactionDAO(self.session)
        return dao.get_total_spend(ba_id, month_start, month_end, assistant_id=agent_id)

    # ------------------------------------------------------------------
    # Inactivity / re-engagement tracking
    # ------------------------------------------------------------------

    def touch_last_correspondence_at(
        self,
        agent_id: int,
        when: datetime,
    ) -> int:
        """Record fresh correspondence activity for an assistant.

        Updates ``last_correspondence_at`` and clears
        ``last_followup_sent_at`` so that a subsequent lapse can fire a
        fresh re-engagement nudge.

        :param agent_id: Assistant agent ID.
        :param when: Timestamp of the correspondence event (tz-aware).
        :return: Number of rows updated (0 if agent_id does not exist).
        """
        result = self.session.execute(
            update(Assistant)
            .where(Assistant.agent_id == agent_id)
            .values(
                last_correspondence_at=when,
                last_followup_sent_at=None,
            ),
        )
        return result.rowcount

    def mark_followup_sent(
        self,
        agent_id: int,
        when: datetime,
    ) -> int:
        """Record that the inactivity follow-up was dispatched.

        :param agent_id: Assistant agent ID.
        :param when: Dispatch timestamp (tz-aware).
        :return: Number of rows updated.
        """
        result = self.session.execute(
            update(Assistant)
            .where(Assistant.agent_id == agent_id)
            .values(last_followup_sent_at=when),
        )
        return result.rowcount

    def set_inactivity_followup_opt_out(
        self,
        agent_id: int,
        opted_out: bool,
    ) -> int:
        """Opt an assistant in or out of inactivity re-engagement follow-ups.

        Setting ``opted_out=True`` excludes this Coordinator from the
        follow-up routine until it is cleared. Called when the boss
        explicitly declines further follow-ups (and again to re-enable
        them if they later re-engage).

        :param agent_id: Assistant agent ID.
        :param opted_out: New opt-out state.
        :return: Number of rows updated (0 if agent_id does not exist).
        """
        result = self.session.execute(
            update(Assistant)
            .where(Assistant.agent_id == agent_id)
            .values(inactivity_followup_opted_out=opted_out),
        )
        return result.rowcount

    def find_followup_candidates(
        self,
        followup_cutoff: datetime,
        limit: Optional[int] = None,
        include_demo: bool = False,
        include_local: bool = False,
    ) -> List[Assistant]:
        """Return personal Coordinators whose owner is due a follow-up.

        This is a *per-user* query: a user is due a re-engagement
        follow-up when they have not interacted with **any** of their
        assistants (the Coordinator included) for ``followup_cutoff``.
        The returned rows are the users' personal Coordinators (the
        assistant that follows up); ``last_followup_sent_at`` on the
        Coordinator row records the last follow-up so we don't re-fire
        every run.

        Activity is the most recent ``last_correspondence_at`` across all
        of a user's assistants. That column carries a ``server_default``
        of ``now()`` at row creation, so a user who signed up and never
        engaged still has a baseline timestamp (their signup time) and is
        followed up with once the window elapses — no separate "never
        engaged" case is needed.

        The follow-up re-arms automatically: once the user engages again
        (any assistant's ``last_correspondence_at`` moves past the
        Coordinator's ``last_followup_sent_at``), a fresh lapse becomes
        eligible. A follow-up already sent after the latest activity is
        not repeated. Coordinators whose owner has opted out
        (``inactivity_followup_opted_out``) are excluded entirely.
        Owners without a non-empty ``User.email`` are excluded (the
        templated email path has nowhere to send).

        :param followup_cutoff: Activity older than this triggers a
            follow-up.
        :param limit: Optional cap on the returned batch.
        :param include_demo: Include demo assistants in the activity
            aggregate and as Coordinators (default: False).
        :param include_local: Include ``is_local=True`` assistants
            (default: False).
        :return: Personal Coordinator rows to follow up with.
        """
        activity_query = select(
            Assistant.user_id.label("user_id"),
            func.max(Assistant.last_correspondence_at).label("last_activity"),
        ).where(Assistant.user_id.isnot(None))
        if not include_demo:
            activity_query = activity_query.where(Assistant.demo_id.is_(None))
        if not include_local:
            activity_query = activity_query.where(Assistant.is_local.is_(False))
        activity_subq = activity_query.group_by(Assistant.user_id).subquery()

        stmt = (
            select(Assistant)
            .join(activity_subq, activity_subq.c.user_id == Assistant.user_id)
            .join(User, User.id == Assistant.user_id)
            .where(
                Assistant.is_coordinator.is_(True),
                Assistant.organization_id.is_(None),
                Assistant.inactivity_followup_opted_out.is_(False),
                User.email.isnot(None),
                User.email != "",
                activity_subq.c.last_activity.isnot(None),
                activity_subq.c.last_activity < followup_cutoff,
                or_(
                    Assistant.last_followup_sent_at.is_(None),
                    Assistant.last_followup_sent_at < activity_subq.c.last_activity,
                ),
            )
        )
        if not include_demo:
            stmt = stmt.where(Assistant.demo_id.is_(None))
        if not include_local:
            stmt = stmt.where(Assistant.is_local.is_(False))
        stmt = stmt.order_by(activity_subq.c.last_activity.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars().all())
