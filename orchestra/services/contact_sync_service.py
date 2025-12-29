"""
Service for syncing User/Assistant profile fields to Contact logs.

When users or assistants update their timezone or bio, this service
propagates those changes to the corresponding Contact log entries
in the "Assistants" project's "All/Contacts" context.
"""

import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.models.orchestra_models import Context, Project

logger = logging.getLogger(__name__)


class ContactSyncService:
    """
    Service for syncing profile fields between User/Assistant and Contact logs.

    Handles:
    - User timezone → Contact logs (first_name + surname, is_system=True)
    - User bio → Contact logs (first_name + surname, is_system=True)
    - Assistant timezone → Contact logs (_assistant=FirstSurname, contact_id=0)
    - Assistant about → Contact logs (_assistant=FirstSurname, contact_id=0)
    """

    ASSISTANTS_PROJECT_NAME = "Assistants"
    CONTACTS_CONTEXT_NAME = "All/Contacts"

    def __init__(self, session: Session):
        self.session = session

    def _get_all_assistants_projects_for_user(self, user_id: str) -> List[Project]:
        """
        Get all "Assistants" projects accessible to a user.

        Returns both:
        - Personal "Assistants" project (if exists)
        - Org "Assistants" projects for all orgs the user belongs to
        """
        projects = []

        # 1. Get personal Assistants project
        personal_project = (
            self.session.query(Project)
            .filter(
                Project.user_id == user_id,
                Project.organization_id.is_(None),
                Project.name == self.ASSISTANTS_PROJECT_NAME,
            )
            .first()
        )
        if personal_project:
            projects.append(personal_project)

        # 2. Get org Assistants projects for all orgs user belongs to
        org_member_dao = OrganizationMemberDAO(self.session)
        memberships = org_member_dao.filter(user_id=user_id)
        org_ids = [m[0].organization_id for m in memberships] if memberships else []

        if org_ids:
            org_projects = (
                self.session.query(Project)
                .filter(
                    Project.organization_id.in_(org_ids),
                    Project.name == self.ASSISTANTS_PROJECT_NAME,
                )
                .all()
            )
            projects.extend(org_projects)

        return projects

    def _get_assistants_project_for_assistant(
        self,
        user_id: str,
        organization_id: Optional[int],
    ) -> Optional[Project]:
        """
        Get the "Assistants" project for an assistant.

        - If org assistant: returns org's Assistants project
        - If personal assistant: returns user's personal Assistants project
        """
        if organization_id is not None:
            # Org assistant - find org's Assistants project
            return (
                self.session.query(Project)
                .filter(
                    Project.organization_id == organization_id,
                    Project.name == self.ASSISTANTS_PROJECT_NAME,
                )
                .first()
            )
        else:
            # Personal assistant - find user's personal Assistants project
            return (
                self.session.query(Project)
                .filter(
                    Project.user_id == user_id,
                    Project.organization_id.is_(None),
                    Project.name == self.ASSISTANTS_PROJECT_NAME,
                )
                .first()
            )

    def _get_contacts_context(self, project_id: int) -> Optional[Context]:
        """Get the All/Contacts context for a project."""
        return (
            self.session.query(Context)
            .filter(
                Context.project_id == project_id,
                Context.name == self.CONTACTS_CONTEXT_NAME,
            )
            .first()
        )

    def _update_contact_logs_user(
        self,
        context_id: int,
        email: str,
        update_field: str,
        new_value: Optional[str],
    ) -> int:
        """
        Update Contact logs for a user (where email_address matches and is_system=True).

        Args:
            context_id: The context ID to search within
            email: The user's email to filter by (matches email_address field in logs)
            update_field: The field name to update (e.g., "timezone", "bio")
            new_value: The new value to set

        Returns:
            Number of logs updated
        """
        query = text(
            """
            UPDATE log_event
            SET data = data || jsonb_build_object(:update_field, :new_value),
                updated_at = NOW()
            WHERE id IN (
                SELECT le.id
                FROM log_event le
                JOIN log_event_context lec ON le.id = lec.log_event_id
                WHERE lec.context_id = :context_id
                  AND le.data->>'email_address' = :email
                  AND (le.data->>'is_system')::boolean = true
            )
        """,
        )

        result = self.session.execute(
            query,
            {
                "context_id": context_id,
                "email": email,
                "update_field": update_field,
                "new_value": new_value,
            },
        )
        return result.rowcount

    def _update_contact_logs_assistant(
        self,
        context_id: int,
        assistant_name: str,
        update_field: str,
        new_value: Optional[str],
    ) -> int:
        """
        Update Contact logs for an assistant (where _assistant matches and contact_id=0).

        Args:
            context_id: The context ID to search within
            assistant_name: The _assistant value to filter by (FirstSurname)
            update_field: The field name to update (e.g., "timezone", "bio")
            new_value: The new value to set

        Returns:
            Number of logs updated
        """
        query = text(
            """
            UPDATE log_event
            SET data = data || jsonb_build_object(:update_field, :new_value),
                updated_at = NOW()
            WHERE id IN (
                SELECT le.id
                FROM log_event le
                JOIN log_event_context lec ON le.id = lec.log_event_id
                WHERE lec.context_id = :context_id
                  AND le.data->>'_assistant' = :assistant_name
                  AND (le.data->>'contact_id')::int = 0
            )
        """,
        )

        result = self.session.execute(
            query,
            {
                "context_id": context_id,
                "assistant_name": assistant_name,
                "update_field": update_field,
                "new_value": new_value,
            },
        )
        return result.rowcount

    # =========================================================================
    # USER SYNC METHODS
    # =========================================================================

    def sync_user_timezone(
        self,
        user_id: str,
        email: str,
        new_timezone: Optional[str],
    ) -> int:
        """
        Sync user timezone to All/Contacts logs.

        Updates logs where:
        - email field matches
        - is_system = True

        Syncs to ALL accessible Assistants projects (personal + all org memberships).

        Args:
            user_id: The user's ID
            email: User's email (matches contact email)
            new_timezone: The new timezone value to set

        Returns:
            Total number of logs updated across all projects
        """
        if not email:
            logger.debug("Skipping user timezone sync: no email available")
            return 0

        total_updated = 0

        # Get all Assistants projects for this user
        projects = self._get_all_assistants_projects_for_user(user_id)

        for project in projects:
            context = self._get_contacts_context(project.id)
            if not context:
                continue

            updated = self._update_contact_logs_user(
                context_id=context.id,
                email=email,
                update_field="timezone",
                new_value=new_timezone,
            )
            total_updated += updated
            if updated > 0:
                logger.debug(
                    f"Synced user timezone to {updated} logs in project {project.id}",
                )

        return total_updated

    def sync_user_bio(
        self,
        user_id: str,
        email: str,
        new_bio: Optional[str],
    ) -> int:
        """
        Sync user bio to All/Contacts logs.

        Updates logs where:
        - email field matches
        - is_system = True

        Syncs to ALL accessible Assistants projects (personal + all org memberships).

        Args:
            user_id: The user's ID
            email: User's email (matches contact email)
            new_bio: The new bio value to set

        Returns:
            Total number of logs updated across all projects
        """
        if not email:
            logger.debug("Skipping user bio sync: no email available")
            return 0

        total_updated = 0

        projects = self._get_all_assistants_projects_for_user(user_id)

        for project in projects:
            context = self._get_contacts_context(project.id)
            if not context:
                continue

            updated = self._update_contact_logs_user(
                context_id=context.id,
                email=email,
                update_field="bio",
                new_value=new_bio,
            )
            total_updated += updated
            if updated > 0:
                logger.debug(
                    f"Synced user bio to {updated} logs in project {project.id}",
                )

        return total_updated

    # =========================================================================
    # ASSISTANT SYNC METHODS
    # =========================================================================

    def sync_assistant_timezone(
        self,
        user_id: str,
        organization_id: Optional[int],
        first_name: Optional[str],
        surname: Optional[str],
        new_timezone: Optional[str],
    ) -> int:
        """
        Sync assistant timezone to All/Contacts logs.

        Updates logs where:
        - _assistant = "{first_name}{surname}"
        - contact_id = 0

        Args:
            user_id: The user ID (owner for personal, creator for org)
            organization_id: The organization ID (None for personal assistants)
            first_name: Assistant's first name
            surname: Assistant's surname
            new_timezone: The new timezone value to set

        Returns:
            Number of logs updated
        """
        if not first_name and not surname:
            logger.debug("Skipping assistant timezone sync: no name available")
            return 0

        assistant_name = f"{first_name or ''}{surname or ''}"

        project = self._get_assistants_project_for_assistant(user_id, organization_id)
        if not project:
            logger.debug("Skipping assistant timezone sync: no Assistants project")
            return 0

        context = self._get_contacts_context(project.id)
        if not context:
            logger.debug("Skipping assistant timezone sync: no All/Contacts context")
            return 0

        updated = self._update_contact_logs_assistant(
            context_id=context.id,
            assistant_name=assistant_name,
            update_field="timezone",
            new_value=new_timezone,
        )
        if updated > 0:
            logger.debug(
                f"Synced assistant timezone to {updated} logs in project {project.id}",
            )
        return updated

    def sync_assistant_bio(
        self,
        user_id: str,
        organization_id: Optional[int],
        first_name: Optional[str],
        surname: Optional[str],
        new_bio: Optional[str],
    ) -> int:
        """
        Sync assistant about/bio to All/Contacts logs.

        Updates logs where:
        - _assistant = "{first_name}{surname}"
        - contact_id = 0

        Args:
            user_id: The user ID (owner for personal, creator for org)
            organization_id: The organization ID (None for personal assistants)
            first_name: Assistant's first name
            surname: Assistant's surname
            new_bio: The new bio value to set

        Returns:
            Number of logs updated
        """
        if not first_name and not surname:
            logger.debug("Skipping assistant bio sync: no name available")
            return 0

        assistant_name = f"{first_name or ''}{surname or ''}"

        project = self._get_assistants_project_for_assistant(user_id, organization_id)
        if not project:
            logger.debug("Skipping assistant bio sync: no Assistants project")
            return 0

        context = self._get_contacts_context(project.id)
        if not context:
            logger.debug("Skipping assistant bio sync: no All/Contacts context")
            return 0

        updated = self._update_contact_logs_assistant(
            context_id=context.id,
            assistant_name=assistant_name,
            update_field="bio",
            new_value=new_bio,
        )
        if updated > 0:
            logger.debug(
                f"Synced assistant bio to {updated} logs in project {project.id}",
            )
        return updated

    def mark_member_contact_as_non_system(
        self,
        organization_id: int,
        email: str,
    ) -> int:
        """
        Mark a departing member's Contact log as non-system (is_system=False).

        Called when a member is removed from an organization. This updates
        their Contact entry in the org's Assistants project's All/Contacts
        context to set is_system=False, indicating they are no longer a
        system user for that organization.

        Args:
            organization_id: The organization ID
            email: User's email (matches contact email)

        Returns:
            Number of logs updated
        """
        if not email:
            logger.debug("Skipping Contact update: no email available")
            return 0

        # Find the org's Assistants project
        project = (
            self.session.query(Project)
            .filter(
                Project.organization_id == organization_id,
                Project.name == self.ASSISTANTS_PROJECT_NAME,
            )
            .first()
        )

        if not project:
            logger.debug(
                f"No Assistants project found for org {organization_id}, "
                "skipping Contact is_system update",
            )
            return 0

        context = self._get_contacts_context(project.id)
        if not context:
            logger.debug(
                f"No All/Contacts context in org {organization_id}'s Assistants project",
            )
            return 0

        # Update is_system to false for this user's Contact row
        query = text(
            """
            UPDATE log_event
            SET data = data || jsonb_build_object('is_system', false),
                updated_at = NOW()
            WHERE id IN (
                SELECT le.id
                FROM log_event le
                JOIN log_event_context lec ON le.id = lec.log_event_id
                WHERE lec.context_id = :context_id
                  AND le.data->>'email_address' = :email
                  AND (le.data->>'is_system')::boolean = true
            )
        """,
        )

        result = self.session.execute(
            query,
            {
                "context_id": context.id,
                "email": email,
            },
        )

        updated = result.rowcount
        if updated > 0:
            logger.info(
                f"Marked {updated} Contact log(s) as non-system for user "
                f"'{email}' in org {organization_id}",
            )
        return updated
