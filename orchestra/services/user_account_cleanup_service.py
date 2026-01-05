"""Service for cleaning up all resources when a user account is deleted.

Handles cleanup of:
- External resources (GCS storage, Twilio phones, Gmail, PubSub topics)
- Cloned voices
- Database records in tables without CASCADE delete
"""

import logging
from dataclasses import dataclass, field
from typing import List

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    CreditCardFingerprint,
    CustomApiKey,
    CustomEndpoint,
    CustomRouter,
    Embedding,
    LocalEndpoint,
    LogEvent,
    LogEventLog,
    Project,
    Query,
    QueryTagAssociation,
    Router,
    Tag,
    Users,
)

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Result of cleanup operation with counts and errors."""

    assistants_cleaned: int = 0
    projects_cleaned: int = 0
    voices_deleted: int = 0
    embeddings_soft_deleted: int = 0
    legacy_records_deleted: dict = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "assistants_cleaned": self.assistants_cleaned,
            "projects_cleaned": self.projects_cleaned,
            "voices_deleted": self.voices_deleted,
            "embeddings_soft_deleted": self.embeddings_soft_deleted,
            "legacy_records_deleted": self.legacy_records_deleted,
            "errors": self.errors,
        }


class UserAccountCleanupService:
    """Handles complete cleanup of user resources before account deletion."""

    def __init__(self, session: Session):
        self.session = session

    def cleanup_all_user_resources(self, user_id: str) -> CleanupResult:
        """
        Clean up all resources associated with a user.

        This must be called BEFORE deleting the auth_user record.
        Order matters due to foreign key dependencies.

        Args:
            user_id: The user ID to clean up resources for.

        Returns:
            CleanupResult with counts of deleted resources and any errors.
        """
        result = CleanupResult()

        # 1. Clean up all user's assistants (external resources)
        self._cleanup_user_assistants(user_id, result)

        # 2. Clean up cloned voices
        self._cleanup_user_voices(user_id, result)

        # 3. Soft-delete embeddings for all user's projects
        self._soft_delete_user_embeddings(user_id, result)

        # 4. Clean up GCS media from user's project logs
        self._cleanup_project_gcs_media(user_id, result)

        # 5. Delete legacy users table dependencies (no CASCADE)
        self._delete_legacy_user_records(user_id, result)

        return result

    def _cleanup_user_assistants(self, user_id: str, result: CleanupResult) -> None:
        """Clean up external resources for all user's assistants."""
        from orchestra.services.bucket_service import BucketService
        from orchestra.settings import settings
        from orchestra.web.api.utils.assistant_infra import (
            delete_email,
            delete_phone_number,
            delete_pubsub_topic,
            stop_jobs,
        )

        assistants = (
            self.session.query(Assistant)
            .filter(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )
            .all()
        )

        for assistant in assistants:
            try:
                # Stop running jobs
                try:
                    stop_jobs(str(assistant.agent_id), self.session)
                except Exception as e:
                    result.errors.append(
                        f"Failed to stop jobs for assistant {assistant.agent_id}: {e}",
                    )

                # Delete GCS profile photo
                if assistant.profile_photo and assistant.profile_photo.startswith(
                    "gs://",
                ):
                    try:
                        bucket_service = BucketService()
                        bucket_service.delete_assistant_file(assistant.profile_photo)
                    except Exception as e:
                        result.errors.append(
                            f"Failed to delete profile photo for assistant {assistant.agent_id}: {e}",
                        )

                # Delete GCS profile video
                if assistant.profile_video and assistant.profile_video.startswith(
                    "gs://",
                ):
                    try:
                        bucket_service = BucketService()
                        bucket_service.delete_assistant_file(assistant.profile_video)
                    except Exception as e:
                        result.errors.append(
                            f"Failed to delete profile video for assistant {assistant.agent_id}: {e}",
                        )

                # Delete pubsub topic
                try:
                    delete_pubsub_topic(
                        str(assistant.agent_id),
                        is_staging=settings.is_staging,
                    )
                except Exception as e:
                    result.errors.append(
                        f"Failed to delete pubsub topic for assistant {assistant.agent_id}: {e}",
                    )

                # Delete phone number
                if assistant.phone:
                    try:
                        delete_phone_number(assistant.phone)
                    except Exception as e:
                        result.errors.append(
                            f"Failed to delete phone for assistant {assistant.agent_id}: {e}",
                        )

                # Delete email
                if assistant.email:
                    try:
                        delete_email(assistant.email)
                    except Exception as e:
                        result.errors.append(
                            f"Failed to delete email for assistant {assistant.agent_id}: {e}",
                        )

                result.assistants_cleaned += 1

            except Exception as e:
                result.errors.append(
                    f"Failed to clean up assistant {assistant.agent_id}: {e}",
                )

    def _cleanup_user_voices(self, user_id: str, result: CleanupResult) -> None:
        """Delete cloned voices from external providers."""
        from orchestra.db.models.orchestra_models import Voice

        voices = (
            self.session.query(Voice)
            .filter(
                Voice.user_id == user_id,
                Voice.is_preset == False,  # noqa: E712
            )
            .all()
        )

        for voice in voices:
            try:
                if voice.provider == "elevenlabs":
                    self._delete_elevenlabs_voice(voice.voice_id, result)
                elif voice.provider == "cartesia":
                    self._delete_cartesia_voice(voice.voice_id, result)
                result.voices_deleted += 1
            except Exception as e:
                result.errors.append(
                    f"Failed to delete voice {voice.voice_id} from {voice.provider}: {e}",
                )

    def _delete_elevenlabs_voice(self, voice_id: str, result: CleanupResult) -> None:
        """Delete a cloned voice from ElevenLabs."""
        try:
            from orchestra.services.elevenlabs_service import ElevenLabsService

            service = ElevenLabsService()
            service.delete_voice(voice_id)
        except Exception as e:
            result.errors.append(f"ElevenLabs voice deletion failed: {e}")

    def _delete_cartesia_voice(self, voice_id: str, result: CleanupResult) -> None:
        """Delete a cloned voice from Cartesia."""
        try:
            from orchestra.services.cartesia_service import CartesiaService

            service = CartesiaService()
            service.delete_voice(voice_id)
        except Exception as e:
            result.errors.append(f"Cartesia voice deletion failed: {e}")

    def _soft_delete_user_embeddings(self, user_id: str, result: CleanupResult) -> None:
        """Soft-delete all embeddings for user's projects."""
        from sqlalchemy import select, update

        # Get all project IDs for this user
        user_projects = (
            self.session.query(Project.id).filter(Project.user_id == user_id).all()
        )
        project_ids = [p.id for p in user_projects]

        if not project_ids:
            return

        # Get log event IDs for these projects
        log_events_subquery = (
            select(LogEvent.id).where(LogEvent.project_id.in_(project_ids)).subquery()
        )

        # Soft-delete embeddings
        soft_delete_result = self.session.execute(
            update(Embedding)
            .where(Embedding.ref_id.in_(select(log_events_subquery.c.id)))
            .values(is_deleted=True),
        )
        result.embeddings_soft_deleted = soft_delete_result.rowcount
        self.session.flush()

    def _cleanup_project_gcs_media(self, user_id: str, result: CleanupResult) -> None:
        """Delete GCS media from all user's project logs."""
        from orchestra.db.dao.context_dao import ContextDAO
        from orchestra.db.dao.log_dao import LogDAO
        from orchestra.db.models.orchestra_models import Log

        user_projects = (
            self.session.query(Project).filter(Project.user_id == user_id).all()
        )

        context_dao = ContextDAO(self.session)
        log_dao = LogDAO(self.session, context_dao)

        for project in user_projects:
            try:
                # Get all log event IDs for this project
                log_events_subquery = (
                    self.session.query(LogEvent.id)
                    .filter(LogEvent.project_id == project.id)
                    .subquery()
                )

                # Get logs with GCS media
                logs_query = (
                    self.session.query(Log)
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .filter(
                        LogEventLog.log_event_id.in_(
                            self.session.query(log_events_subquery.c.id),
                        ),
                    )
                )

                log_dao._bulk_delete_gcs_media(logs_query)
                result.projects_cleaned += 1

            except Exception as e:
                result.errors.append(
                    f"Failed to clean GCS media for project {project.id}: {e}",
                )

    def _delete_legacy_user_records(self, user_id: str, result: CleanupResult) -> None:
        """Delete records from legacy users table dependencies (no CASCADE)."""
        legacy_deleted = {
            "custom_endpoints": 0,
            "custom_api_keys": 0,
            "custom_routers": 0,
            "credit_card_fingerprints": 0,
            "query_tag_associations": 0,
            "tags": 0,
            "local_endpoints": 0,
            "queries": 0,
            "routers": 0,
        }

        try:
            # Order matters: delete dependent tables first

            # Delete custom_endpoints (depends on custom_api_key)
            legacy_deleted["custom_endpoints"] = (
                self.session.query(CustomEndpoint)
                .filter(CustomEndpoint.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete custom_api_keys
            legacy_deleted["custom_api_keys"] = (
                self.session.query(CustomApiKey)
                .filter(CustomApiKey.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete custom_routers
            legacy_deleted["custom_routers"] = (
                self.session.query(CustomRouter)
                .filter(CustomRouter.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete credit_card_fingerprints
            legacy_deleted["credit_card_fingerprints"] = (
                self.session.query(CreditCardFingerprint)
                .filter(CreditCardFingerprint.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete query_tag_associations (depends on tags)
            legacy_deleted["query_tag_associations"] = (
                self.session.query(QueryTagAssociation)
                .filter(QueryTagAssociation.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete tags
            legacy_deleted["tags"] = (
                self.session.query(Tag)
                .filter(Tag.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete local_endpoints
            legacy_deleted["local_endpoints"] = (
                self.session.query(LocalEndpoint)
                .filter(LocalEndpoint.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete queries
            legacy_deleted["queries"] = (
                self.session.query(Query)
                .filter(Query.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete routers
            legacy_deleted["routers"] = (
                self.session.query(Router)
                .filter(Router.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete the users record itself
            self.session.query(Users).filter(Users.id == user_id).delete(
                synchronize_session="fetch",
            )

            self.session.flush()
            result.legacy_records_deleted = legacy_deleted

        except Exception as e:
            result.errors.append(f"Failed to delete legacy user records: {e}")
            raise

    def check_deletion_blockers(self, user_id: str) -> List[str]:
        """
        Check for conditions that block account deletion.

        Returns:
            List of blocking reasons. Empty list means deletion is allowed.
        """
        from orchestra.db.models.orchestra_models import Organization

        blockers = []

        # Check if user is the owner of any organization
        owned_orgs = (
            self.session.query(Organization)
            .filter(Organization.owner_id == user_id)
            .all()
        )

        if owned_orgs:
            org_names = [org.name for org in owned_orgs]
            blockers.append(
                f"User owns {len(owned_orgs)} organization(s): {', '.join(org_names)}. "
                "Transfer ownership or delete these organizations first.",
            )

        return blockers
