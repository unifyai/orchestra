"""
Service for syncing User/Assistant profile fields to Contact logs.

When users or assistants update their profile fields (timezone, bio,
first_name, surname), this service propagates those changes to the
corresponding Contact log entries.

Contacts live in each assistant's own owner-homogeneous Contacts context
(``{user_id}/{agent_id}/Contacts``). A profile change is therefore fanned out
across the relevant assistants' own Contacts contexts: a user's row (matched by
email) is updated in every assistant that knows them; an assistant's self row
(matched by its resolved self ``contact_id``) is updated in its own context.

Each per-context update is pruned to the project's ``LIST(project_id)``
partition and, within the shared Assistants project, to the assistant's
``owner_key`` sub-partition.
"""

import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    ContactMembership,
    Context,
    Project,
)
from orchestra.db.scope import OwnerScope, single_owner_key

logger = logging.getLogger(__name__)


class ContactSyncService:
    """
    Service for syncing profile fields between User/Assistant and Contact logs.

    Handles:
    - User timezone/bio -> Contact rows (matched by email, is_system=True) in
      every assistant's own Contacts context the user appears in.
    - Assistant timezone/bio/first_name/surname -> the assistant's self Contact
      row (matched by resolved self contact_id) in its own Contacts context.
    - Member removal -> flips is_system=False on the departing member's Contact
      rows across the org's assistants' Contacts contexts.
    """

    ASSISTANTS_PROJECT_NAME = "Assistants"
    # Per-assistant contacts contexts are named ``{user_id}/{agent_id}/Contacts``.
    CONTACTS_CONTEXT_SUFFIX = "/Contacts"

    def __init__(self, session: Session):
        self.session = session
        self._self_contact_id_cache: dict[int, Optional[int]] = {}

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

    def _assistant_contacts_contexts(self, project_id: int) -> List[Context]:
        """All per-assistant Contacts contexts in a project.

        Each is owner-homogeneous (``owner_scope='assistant'``), so updates
        through them prune to a single owner sub-partition.
        """
        return (
            self.session.query(Context)
            .filter(
                Context.project_id == project_id,
                Context.owner_scope == OwnerScope.ASSISTANT.value,
                Context.name.like(f"%{self.CONTACTS_CONTEXT_SUFFIX}"),
            )
            .all()
        )

    def _assistant_own_contacts_context(
        self,
        project_id: int,
        agent_id: int,
    ) -> Optional[Context]:
        """The Contacts context owned by a specific assistant in a project."""
        return (
            self.session.query(Context)
            .filter(
                Context.project_id == project_id,
                Context.owner_scope == OwnerScope.ASSISTANT.value,
                Context.owner_id == agent_id,
                Context.name.like(f"%{self.CONTACTS_CONTEXT_SUFFIX}"),
            )
            .first()
        )

    def _resolve_assistant_self_contact_id(self, agent_id: int) -> Optional[int]:
        """Return the assistant's personal self contact id from membership overlays."""
        if agent_id in self._self_contact_id_cache:
            return self._self_contact_id_cache[agent_id]

        membership = (
            self.session.query(ContactMembership)
            .filter(
                ContactMembership.assistant_id == agent_id,
                ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                ContactMembership.relationship == CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            )
            .order_by(ContactMembership.id)
            .first()
        )
        if membership is None:
            logger.error(
                "Skipping assistant contact sync: missing self contact overlay "
                "for assistant %s",
                agent_id,
            )
            self._self_contact_id_cache[agent_id] = None
            return None
        self_contact_id = membership.contact_id
        self._self_contact_id_cache[agent_id] = self_contact_id
        return self_contact_id

    # =========================================================================
    # Per-context updates (project_id + owner_key pruned)
    # =========================================================================

    def _set_field_where(
        self,
        context: Context,
        set_pairs: str,
        match_clause: str,
        params: dict,
    ) -> int:
        """Run a partition-pruned UPDATE of ``log_event.data`` in one context.

        ``set_pairs`` is the ``jsonb_build_object(...)`` payload merged into
        ``data``; ``match_clause`` is the extra ``data``-field predicate that
        selects the row(s). ``project_id`` (and, where the project is owner
        sub-partitioned, ``owner_key``) are pinned to literals so the scan
        prunes instead of fanning out.
        """
        owner_key = single_owner_key(context.owner_scope, context.owner_id)
        owner_outer = "AND owner_key = :owner_key\n" if owner_key is not None else ""
        owner_inner = "AND le.owner_key = :owner_key\n" if owner_key is not None else ""
        query = text(
            f"""
            UPDATE log_event
            SET data = data || {set_pairs},
                updated_at = NOW()
            WHERE project_id = :project_id
              {owner_outer}AND id IN (
                SELECT le.id
                FROM log_event le
                JOIN log_event_context lec ON le.id = lec.log_event_id
                  AND lec.project_id = le.project_id
                WHERE le.project_id = :project_id
                  {owner_inner}AND lec.context_id = :context_id
                  {match_clause}
            )
        """,
        )
        bound = {
            "project_id": context.project_id,
            "context_id": context.id,
            **params,
        }
        if owner_key is not None:
            bound["owner_key"] = owner_key
        return self.session.execute(query, bound).rowcount

    def _update_user_contact_row(
        self,
        context: Context,
        email: str,
        update_field: str,
        new_value: Optional[str],
    ) -> int:
        """Update the user's (is_system) Contact row in one context, by email."""
        return self._set_field_where(
            context,
            set_pairs="jsonb_build_object(:update_field, :new_value)",
            match_clause=(
                "AND le.data->>'email_address' = :email "
                "AND (le.data->>'is_system')::boolean = true"
            ),
            params={
                "update_field": update_field,
                "new_value": new_value,
                "email": email,
            },
        )

    def _update_assistant_self_row(
        self,
        context: Context,
        self_contact_id: int,
        update_field: str,
        new_value: Optional[str],
    ) -> int:
        """Update the assistant's self Contact row in its own context."""
        return self._set_field_where(
            context,
            set_pairs="jsonb_build_object(:update_field, :new_value)",
            match_clause="AND (le.data->>'contact_id')::int = :self_contact_id",
            params={
                "update_field": update_field,
                "new_value": new_value,
                "self_contact_id": self_contact_id,
            },
        )

    # =========================================================================
    # USER SYNC METHODS
    # =========================================================================

    def _sync_user_field(
        self,
        user_id: str,
        email: str,
        update_field: str,
        new_value: Optional[str],
    ) -> int:
        """Fan a user profile field out to their Contact row in every assistant's
        Contacts context, across all the user's accessible Assistants projects."""
        if not email:
            logger.debug("Skipping user %s sync: no email available", update_field)
            return 0

        total_updated = 0
        for project in self._get_all_assistants_projects_for_user(user_id):
            for context in self._assistant_contacts_contexts(project.id):
                updated = self._update_user_contact_row(
                    context=context,
                    email=email,
                    update_field=update_field,
                    new_value=new_value,
                )
                total_updated += updated
        if total_updated:
            logger.debug(
                "Synced user %s to %d contact rows",
                update_field,
                total_updated,
            )
        return total_updated

    def sync_user_timezone(
        self,
        user_id: str,
        email: str,
        new_timezone: Optional[str],
    ) -> int:
        """Sync user timezone to their Contact rows across all assistants."""
        return self._sync_user_field(user_id, email, "timezone", new_timezone)

    def sync_user_bio(
        self,
        user_id: str,
        email: str,
        new_bio: Optional[str],
    ) -> int:
        """Sync user bio to their Contact rows across all assistants."""
        return self._sync_user_field(user_id, email, "bio", new_bio)

    # =========================================================================
    # ASSISTANT SYNC METHODS
    # =========================================================================

    def _sync_assistant_field(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        update_field: str,
        new_value: Optional[str],
    ) -> int:
        """Update the assistant's self Contact row in its own Contacts context."""
        project = self._get_assistants_project_for_assistant(user_id, organization_id)
        if not project:
            logger.debug(
                "Skipping assistant %s sync: no Assistants project",
                update_field,
            )
            return 0

        context = self._assistant_own_contacts_context(project.id, agent_id)
        if not context:
            logger.debug(
                "Skipping assistant %s sync: no Contacts context for assistant %s",
                update_field,
                agent_id,
            )
            return 0

        self_contact_id = self._resolve_assistant_self_contact_id(agent_id)
        if self_contact_id is None:
            return 0

        updated = self._update_assistant_self_row(
            context=context,
            self_contact_id=self_contact_id,
            update_field=update_field,
            new_value=new_value,
        )
        if updated:
            logger.debug(
                "Synced assistant %s to %d logs in project %s",
                update_field,
                updated,
                project.id,
            )
        return updated

    def sync_assistant_timezone(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        new_timezone: Optional[str],
    ) -> int:
        """Sync assistant timezone to its self Contact row."""
        return self._sync_assistant_field(
            user_id,
            organization_id,
            agent_id,
            "timezone",
            new_timezone,
        )

    def sync_assistant_bio(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        new_bio: Optional[str],
    ) -> int:
        """Sync assistant about/bio to its self Contact row."""
        return self._sync_assistant_field(
            user_id,
            organization_id,
            agent_id,
            "bio",
            new_bio,
        )

    def sync_assistant_first_name(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        new_first_name: Optional[str],
    ) -> int:
        """Sync assistant first_name to its self Contact row."""
        return self._sync_assistant_field(
            user_id,
            organization_id,
            agent_id,
            "first_name",
            new_first_name,
        )

    def sync_assistant_surname(
        self,
        user_id: str,
        organization_id: Optional[int],
        agent_id: int,
        new_surname: Optional[str],
    ) -> int:
        """Sync assistant surname to its self Contact row."""
        return self._sync_assistant_field(
            user_id,
            organization_id,
            agent_id,
            "surname",
            new_surname,
        )

    def mark_member_contact_as_non_system(
        self,
        organization_id: int,
        email: str,
    ) -> int:
        """
        Mark a departing member's Contact rows as non-system (is_system=False).

        Called when a member is removed from an organization. Flips
        ``is_system`` to false on that member's Contact row across every
        assistant's Contacts context in the org's Assistants project, so a
        departed person is no longer treated as a system user there.
        """
        if not email:
            logger.debug("Skipping Contact update: no email available")
            return 0

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
                "No Assistants project found for org %s, skipping Contact "
                "is_system update",
                organization_id,
            )
            return 0

        total_updated = 0
        for context in self._assistant_contacts_contexts(project.id):
            total_updated += self._set_field_where(
                context,
                set_pairs="jsonb_build_object('is_system', false)",
                match_clause=(
                    "AND le.data->>'email_address' = :email "
                    "AND (le.data->>'is_system')::boolean = true"
                ),
                params={"email": email},
            )

        if total_updated:
            logger.info(
                "Marked %d Contact log(s) as non-system for user '%s' in org %s",
                total_updated,
                email,
                organization_id,
            )
        return total_updated
