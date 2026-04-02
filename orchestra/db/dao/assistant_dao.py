from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import available_timezones

from fastapi import HTTPException, status
from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, AssistantContact, User


@dataclass
class AssistantSpendingCapResult:
    """Result of setting an assistant spending cap."""

    monthly_spending_cap: Optional[float] = None
    effective_limit: Optional[float] = None
    parent_limit: Optional[float] = None


VALID_TIMEZONES = available_timezones()


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
        user_desktop_id: Optional[int] = None,
        user_desktop_filesys_sync: bool = False,
        voice_id: Optional[str] = None,
        voice_provider: Optional[str] = None,
        timezone: Optional[str] = None,
        organization_id: Optional[int] = None,
        is_local: bool = False,
        deploy_env: str | None = None,
    ) -> Assistant:
        """
        Create a new Assistant.

        If organization_id is provided, creates an organizational assistant.
        If organization_id is None, creates a personal assistant.

        :param user_id: Personal assistants: owner. Org assistants:
            creator/lifecycle owner retained on the row.
        :param organization_id: Optional organization scope for org assistants.
            None means a personal assistant.
        :return: The created Assistant.
        """

        if timezone is not None and timezone not in VALID_TIMEZONES:
            raise ValueError(f"'{timezone}' is not a valid IANA timezone.")

        assistant = Assistant(
            user_id=user_id,
            organization_id=organization_id,
            first_name=first_name,
            surname=surname,
            age=age,
            nationality=nationality,
            profile_photo=profile_photo,
            profile_video=profile_video,
            desktop_mode=desktop_mode,
            user_desktop_id=user_desktop_id,
            user_desktop_filesys_sync=user_desktop_filesys_sync,
            about=about,
            weekly_limit=weekly_limit,
            max_parallel=max_parallel,
            voice_id=voice_id,
            voice_provider=voice_provider,
            timezone=timezone,
            is_local=is_local,
            deploy_env=deploy_env,
        )
        self.session.add(assistant)
        self.session.flush()
        return assistant

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

    def list_assistants_for_user(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
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

        if phone is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "phone",
                        AssistantContact.contact_value == phone,
                        AssistantContact.status != "deleted",
                    ),
                ),
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
        if email is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "email",
                        AssistantContact.contact_value == email,
                        AssistantContact.status != "deleted",
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
        if assistant_whatsapp_number is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "whatsapp",
                        AssistantContact.contact_value == assistant_whatsapp_number,
                        AssistantContact.status != "deleted",
                    ),
                ),
            )
        result = self.session.execute(stmt).scalars().all()
        return result

    def list_all_org_assistants(
        self,
        organization_id: int,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
        include_demo: bool = False,
        demo_only: bool = False,
    ) -> List[Assistant]:
        """
        List ALL assistants in an organization (for list_all_org=True).

        This returns all assistants in the org, regardless of who created them.
        Should only be called after verifying the user has assistant:read permission.

        :param organization_id: Organization ID.
        :param include_demo: If True, include demo assistants in results.
        :param demo_only: If True, only return demo assistants.
        :return: List of all assistants in the organization.
        """
        stmt = select(Assistant).where(
            Assistant.organization_id == organization_id,
        )

        # Demo filtering
        if demo_only:
            stmt = stmt.where(Assistant.demo_id.isnot(None))
        elif not include_demo:
            stmt = stmt.where(Assistant.demo_id.is_(None))

        if phone is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "phone",
                        AssistantContact.contact_value == phone,
                        AssistantContact.status != "deleted",
                    ),
                ),
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
        if email is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "email",
                        AssistantContact.contact_value == email,
                        AssistantContact.status != "deleted",
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
        if assistant_whatsapp_number is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "whatsapp",
                        AssistantContact.contact_value == assistant_whatsapp_number,
                        AssistantContact.status != "deleted",
                    ),
                ),
            )
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
    ) -> List[Assistant]:
        """
        List all Assistants across all users with optional filtering.

        Contact filters (phone, email, whatsapp) are resolved via the
        ``assistant_contacts`` table rather than the legacy columns on
        the ``assistants`` table.

        This is an admin-level function that returns all assistants.
        """
        stmt = select(Assistant)
        if phone is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "phone",
                        AssistantContact.contact_value == phone,
                        AssistantContact.status != "deleted",
                    ),
                ),
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
        if assistant_whatsapp_number is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "whatsapp",
                        AssistantContact.contact_value == assistant_whatsapp_number,
                        AssistantContact.status != "deleted",
                    ),
                ),
            )
        if email is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        AssistantContact.assistant_id == Assistant.agent_id,
                        AssistantContact.contact_type == "email",
                        AssistantContact.contact_value == email,
                        AssistantContact.status != "deleted",
                    ),
                ),
            )
        if agent_id is not None:
            stmt = stmt.where(Assistant.agent_id == agent_id)
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

        # Verify ownership
        if assistant.user_id != user_id:
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

            # Get applicable limits (member limit and org limit)
            member_limit = (
                float(member.monthly_spending_cap)
                if member and member.monthly_spending_cap is not None
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

        Queries the Assistants project logs for spending data.

        :param agent_id: Assistant agent ID.
        :param month: Month in YYYY-MM format.
        :return: Cumulative spend for the month (0.0 if no spend data).
        """
        from sqlalchemy import cast, func
        from sqlalchemy.types import Float

        from orchestra.db.models.orchestra_models import (
            Context,
            LogEvent,
            LogEventContext,
            Project,
        )

        assistant = self.get_assistant_by_agent_id(agent_id)
        if not assistant:
            return 0.0

        # Build query based on whether assistant is personal or organizational
        query = (
            self.session.query(
                func.coalesce(
                    cast(LogEvent.data.op("->>")("cumulative_spend"), Float),
                    0.0,
                ).label("spend"),
            )
            .select_from(LogEvent)
            .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
            .join(Context, LogEventContext.context_id == Context.id)
            .join(Project, Context.project_id == Project.id)
            .filter(
                Project.name == "Assistants",
                Context.name == "All/Spending/Monthly",
                LogEvent.data.op("->>")("_assistant_id") == str(agent_id),
                LogEvent.data.op("->>")("month") == month,
            )
        )

        # Add project ownership filter based on whether assistant is personal or org
        if assistant.organization_id:
            query = query.filter(Project.organization_id == assistant.organization_id)
        else:
            query = query.filter(
                Project.organization_id.is_(None),
                Project.user_id == assistant.user_id,
            )

        result = query.first()

        if result and result.spend:
            return float(result.spend)
        return 0.0

    def generate_unique_email_local(
        self,
        first_name: str,
        surname: str,
    ) -> str:
        """
        Generate a unique email local part for demo assistants.

        Uses {first_name.lower()}.{surname.lower()} as base.
        If a collision exists, appends .1, .2, etc. until unique.

        :param first_name: Assistant's first name
        :param surname: Assistant's surname
        :return: Unique email local part (without @domain)
        """
        import re

        # Normalize names: lowercase, remove non-alphanumeric, limit length
        def normalize(s: str) -> str:
            s = re.sub(r"[^a-z0-9]", "", s.lower())
            return s[:30] if len(s) > 30 else s

        base_local = f"{normalize(first_name)}.{normalize(surname)}"

        # Query existing emails from AssistantContact to check for collisions
        existing_emails = (
            self.session.query(AssistantContact.contact_value)
            .filter(
                AssistantContact.contact_type == "email",
                AssistantContact.status != "deleted",
                AssistantContact.contact_value.isnot(None),
            )
            .all()
        )
        existing_locals = {
            email[0].lower().split("@")[0] for email in existing_emails if email[0]
        }

        # Check if base is unique
        if base_local not in existing_locals:
            return base_local

        # Find unique suffix
        for i in range(1, 1000):
            candidate = f"{base_local}.{i}"
            if candidate not in existing_locals:
                return candidate

        # Extremely unlikely fallback
        import uuid

        return f"{base_local}.{uuid.uuid4().hex[:8]}"
