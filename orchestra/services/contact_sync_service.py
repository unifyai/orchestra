"""
Service for syncing User/Assistant profile fields to Contact logs.

When users or assistants update their profile fields (timezone, bio,
first_name, surname), this service propagates those changes to the
``Contacts`` log row that represents the editing entity inside the
``Assistants`` project.

Two Contact roots are addressed depending on the body's Hive
membership:

* solo bodies write ``{user_id}/{assistant_id}/Contacts`` and the row
  is referenced from the project-level ``All/Contacts`` archive — a
  single ``UPDATE … JOIN log_event_context`` on that archive reaches
  the underlying ``log_event`` row.
* Hive members write ``Hives/{hive_id}/Contacts`` directly; those rows
  do not aggregate into ``All/Contacts`` (Hive paths short-circuit
  archival), so updates target the Hive context by name.

The assistant's self-row is identified by ``assistant_id == agent_id``
on the shared ``Contacts`` table — the stable per-body marker. User
self-rows are identified by ``email_address`` plus ``is_system = true``.
"""

import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestra.db.context_naming import HIVE_CONTEXT_PREFIX
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.models.orchestra_models import Context, Project

logger = logging.getLogger(__name__)


class ContactSyncService:
    """Sync profile fields between User/Assistant rows and Contact logs.

    Handles:

    * User timezone/bio → Contact logs matched by email + ``is_system``.
    * Assistant timezone/about/first_name/surname → the ``Contacts`` row
      whose ``assistant_id`` field names the body (Hive-aware: shared
      Hive Contacts for Hive members, the project ``All/Contacts``
      archive for solo bodies).
    """

    ASSISTANTS_PROJECT_NAME = "Assistants"
    SOLO_CONTACTS_CONTEXT_NAME = "All/Contacts"

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

    def _get_contacts_context(
        self,
        project_id: int,
        context_name: Optional[str] = None,
    ) -> Optional[Context]:
        """Look up a Contacts context within an Assistants project.

        ``context_name`` defaults to the solo aggregation archive
        (``All/Contacts``); Hive callers pass ``Hives/{hive_id}/Contacts``
        to address the shared root directly.
        """
        return (
            self.session.query(Context)
            .filter(
                Context.project_id == project_id,
                Context.name == (context_name or self.SOLO_CONTACTS_CONTEXT_NAME),
            )
            .first()
        )

    def _resolve_assistant_contacts_context_name(
        self,
        hive_id: Optional[int],
    ) -> str:
        """Pick the Contacts context where this body's self-row lives.

        Hive members write to ``Hives/{hive_id}/Contacts`` directly and
        bypass the project ``All/Contacts`` archive; solo bodies live in
        that archive via reference. Returning the right name keeps the
        ``UPDATE`` join reachable for both shapes.
        """
        if hive_id is not None:
            return f"{HIVE_CONTEXT_PREFIX}{hive_id}/Contacts"
        return self.SOLO_CONTACTS_CONTEXT_NAME

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
        agent_id: int,
        update_field: str,
        new_value: Optional[str],
    ) -> int:
        """Update the Contacts row that represents an assistant body.

        The body's self-row is identified by ``assistant_id == agent_id``
        — the shared ``Contacts`` table sets that field only on the row
        the body owns, so the filter is identity-based. Aggregation
        contexts hold log references, not copies, so a single ``UPDATE``
        on ``log_event`` propagates to every view that points at the
        same log id.

        Args:
            context_id: Contacts context to scope the join through.
            agent_id: The body's ``Assistant.agent_id``.
            update_field: Field name to overwrite (``timezone`` etc.).
            new_value: New value to write.

        Returns:
            Number of ``log_event`` rows updated.
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
                  AND (le.data->>'assistant_id')::int = :agent_id
            )
        """,
        )

        result = self.session.execute(
            query,
            {
                "context_id": context_id,
                "agent_id": agent_id,
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

    def _sync_assistant_field(
        self,
        *,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        hive_id: Optional[int],
        field: str,
        value: Optional[str],
    ) -> int:
        """Update a single field on the body's self-row in Contacts.

        Resolves the Assistants project for this body, picks the right
        Contacts context (Hive root for Hive members, project archive
        for solo bodies), and rewrites the named field on the row whose
        ``assistant_id`` equals ``agent_id``. Missing project, context,
        or row is logged at debug and treated as a no-op so callers can
        invoke this eagerly on every profile change.
        """
        project = self._get_assistants_project_for_assistant(user_id, organization_id)
        if not project:
            logger.debug(
                "Skipping assistant %s sync: no Assistants project",
                field,
            )
            return 0

        context_name = self._resolve_assistant_contacts_context_name(hive_id)
        context = self._get_contacts_context(project.id, context_name=context_name)
        if not context:
            logger.debug(
                "Skipping assistant %s sync: no %s context in project %s",
                field,
                context_name,
                project.id,
            )
            return 0

        updated = self._update_contact_logs_assistant(
            context_id=context.id,
            agent_id=agent_id,
            update_field=field,
            new_value=value,
        )
        if updated > 0:
            logger.debug(
                "Synced assistant %s to %d logs in project %s (%s)",
                field,
                updated,
                project.id,
                context_name,
            )
        return updated

    def sync_assistant_timezone(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        new_timezone: Optional[str],
        hive_id: Optional[int] = None,
    ) -> int:
        """Propagate an assistant timezone update into its Contacts row."""
        return self._sync_assistant_field(
            user_id=user_id,
            organization_id=organization_id,
            agent_id=agent_id,
            hive_id=hive_id,
            field="timezone",
            value=new_timezone,
        )

    def sync_assistant_bio(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        new_bio: Optional[str],
        hive_id: Optional[int] = None,
    ) -> int:
        """Propagate an assistant ``about`` update into its Contacts row."""
        return self._sync_assistant_field(
            user_id=user_id,
            organization_id=organization_id,
            agent_id=agent_id,
            hive_id=hive_id,
            field="bio",
            value=new_bio,
        )

    def sync_assistant_first_name(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        new_first_name: Optional[str],
        hive_id: Optional[int] = None,
    ) -> int:
        """Propagate an assistant first-name update into its Contacts row."""
        return self._sync_assistant_field(
            user_id=user_id,
            organization_id=organization_id,
            agent_id=agent_id,
            hive_id=hive_id,
            field="first_name",
            value=new_first_name,
        )

    def sync_assistant_surname(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        new_surname: Optional[str],
        hive_id: Optional[int] = None,
    ) -> int:
        """Propagate an assistant surname update into its Contacts row."""
        return self._sync_assistant_field(
            user_id=user_id,
            organization_id=organization_id,
            agent_id=agent_id,
            hive_id=hive_id,
            field="surname",
            value=new_surname,
        )

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
