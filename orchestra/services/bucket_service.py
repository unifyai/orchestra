import base64
import datetime
import hashlib
import logging
import mimetypes
import os
import uuid
from typing import Optional, Tuple

from google.api_core import exceptions
from google.cloud import storage
from google.oauth2 import service_account

from orchestra.settings import settings
from orchestra.web.api.utils.gcp import parse_gcs_url


class BucketService:
    def __init__(self):
        """Initialize the bucket service with GCP credentials and bucket configuration."""

        # GCP credentials
        self.credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not self.credentials_path:
            raise ValueError(
                "Missing required GCP credentials (set GOOGLE_APPLICATION_CREDENTIALS)",
            )

        # GCP project
        self.project_id = os.getenv("GCP_PROJECT_ID")
        if not self.project_id:
            raise ValueError(
                "Missing required GCP project ID (set GCP_PROJECT_ID)",
            )

        self.credentials = service_account.Credentials.from_service_account_file(
            self.credentials_path,
        )
        self.storage_client = storage.Client(
            project=self.project_id,
            credentials=self.credentials,
        )

        # Generic bucket
        self.bucket_name = os.getenv("ORCHESTRA_GCP_BUCKET_NAME")
        if not self.bucket_name:
            raise ValueError(
                "Missing required GCP configuration (ORCHESTRA_GCP_BUCKET_NAME)",
            )
        self.bucket = self.storage_client.bucket(self.bucket_name)

        # -----------------------------------------------------------------
        # Assistant media bucket (renamed from hired_assistants_images)
        # Env var: ORCHESTRA_GCP_ASSISTANT_MEDIA_BUCKET_NAME
        # Fallback: ORCHESTRA_GCP_ASSISTANT_IMAGES_BUCKET_NAME (deprecated)
        # -----------------------------------------------------------------
        self.assistant_media_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_MEDIA_BUCKET_NAME",
            os.getenv(
                "ORCHESTRA_GCP_ASSISTANT_IMAGES_BUCKET_NAME",
                f"assistant-media-{"staging" if settings.is_staging else "production"}",
            ),
        )
        if not self.assistant_media_bucket_name:
            raise ValueError(
                "Missing required GCP assistant media bucket name configuration "
                "(ORCHESTRA_GCP_ASSISTANT_MEDIA_BUCKET_NAME)",
            )
        self.assistant_media_bucket = self.storage_client.bucket(
            self.assistant_media_bucket_name,
        )

        # -----------------------------------------------------------------
        # Message attachments bucket (renamed from unify-message-attachments)
        # Env var: ORCHESTRA_GCP_ASSISTANT_MESSAGE_ATTACHMENTS_BUCKET_NAME
        # Fallback: ORCHESTRA_GCP_UNIFY_ATTACHMENTS_BUCKET_NAME (deprecated)
        # -----------------------------------------------------------------
        self.message_attachments_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_MESSAGE_ATTACHMENTS_BUCKET_NAME",
            os.getenv(
                "ORCHESTRA_GCP_UNIFY_ATTACHMENTS_BUCKET_NAME",
                f"assistant-message-attachments-{"staging" if settings.is_staging else "production"}",
            ),
        )
        self.message_attachments_bucket = self.storage_client.bucket(
            self.message_attachments_bucket_name,
        )

        # -----------------------------------------------------------------
        # Call recordings bucket (renamed from unity-call-recordings)
        # Env var: ORCHESTRA_GCP_ASSISTANT_CALL_RECORDINGS_BUCKET_NAME
        # Fallback: ORCHESTRA_GCP_RECORDINGS_BUCKET_NAME (deprecated)
        # -----------------------------------------------------------------
        self.call_recordings_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_CALL_RECORDINGS_BUCKET_NAME",
            os.getenv(
                "ORCHESTRA_GCP_RECORDINGS_BUCKET_NAME",
                f"assistant-call-recordings-{"staging" if settings.is_staging else "production"}",
            ),
        )
        self.call_recordings_bucket = self.storage_client.bucket(
            self.call_recordings_bucket_name,
        )

        # -----------------------------------------------------------------
        # Account photo bucket (user + org profile photos)
        # Env var: ORCHESTRA_GCP_ACCOUNT_PHOTO_BUCKET_NAME
        # Defaults: account-photo-staging / account-photo-production
        # -----------------------------------------------------------------
        self.account_photo_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ACCOUNT_PHOTO_BUCKET_NAME",
            f"account-photo-{"staging" if settings.is_staging else "production"}",
        )
        self.account_photo_bucket = self.storage_client.bucket(
            self.account_photo_bucket_name,
        )

        # -----------------------------------------------------------------
        # Presets bucket (environment-agnostic, shared across envs)
        # -----------------------------------------------------------------
        self.presets_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_MEDIA_PRESETS_BUCKET_NAME",
            "assistant-media-presets",
        )
        self.presets_bucket = self.storage_client.bucket(self.presets_bucket_name)

        # -----------------------------------------------------------------
        # Backward-compatible aliases (deprecated, will be removed)
        # These allow existing callers / tests to keep working during migration.
        # -----------------------------------------------------------------
        self.assistant_images_bucket_name = self.assistant_media_bucket_name
        self.assistant_images_bucket = self.assistant_media_bucket
        self.unify_attachments_bucket_name = self.message_attachments_bucket_name
        self.unify_attachments_bucket = self.message_attachments_bucket
        self.recordings_bucket_name = self.call_recordings_bucket_name
        self.recordings_bucket = self.call_recordings_bucket

    # -----------------------------------------------------------------
    #                          Path helpers
    # -----------------------------------------------------------------

    @staticmethod
    def build_assistant_path(
        assistant_id: str | int,
        media_type: str,
        filename: str,
    ) -> str:
        """
        Build an assistant-centric GCS object path.

        Standard path layout:  {assistant_id}/{media_type}/{filename}

        Args:
            assistant_id: The assistant's ID.
            media_type: Category subfolder – one of 'image', 'voice', 'video',
                        'calls', 'attachments'.
            filename: The final filename (with extension).

        Returns:
            The fully-qualified object path within a bucket.
        """
        return f"{assistant_id}/{media_type}/{filename}"

    def _generate_unique_filename(self, content: bytes, extension: str = "") -> str:
        """Generate a unique filename using content hash, UUID, and an optional extension."""
        content_hash = hashlib.md5(content).hexdigest()
        unique_id = str(uuid.uuid4())[:8]
        if extension:
            return f"{content_hash}_{unique_id}.{extension.lstrip('.')}"
        return f"{content_hash}_{unique_id}"

    @staticmethod
    def _media_type_from_content_type(content_type: str) -> str:
        """Derive the media-type subfolder name from a MIME content type."""
        if not content_type:
            return "image"
        ct = content_type.lower()
        if ct.startswith("video/"):
            return "video"
        if ct.startswith("audio/"):
            return "voice"
        return "image"

    # -----------------------------------------------------------------
    #                   General media operations
    # -----------------------------------------------------------------
    def upload_media(self, base64_media: str, media_type: str) -> Tuple[str, str]:
        """
        Upload a base64 encoded media to the bucket.

        Args:
            base64_media: Base64 encoded media string
            media_type: The MIME type of the media

        Returns:
            Tuple containing the media URL and filename

        Raises:
            ValueError: If the base64 media is invalid
            Exception: If upload fails
        """
        try:
            # Remove potential base64 prefix
            if "," in base64_media:
                base64_media = base64_media.split(",")[1]

            # Decode base64 media
            media_content = base64.b64decode(base64_media)

            # Guess the extension from the media type
            extension = mimetypes.guess_extension(media_type) or ""

            # Generate unique filename with extension
            filename = self._generate_unique_filename(media_content, extension)

            # Upload to GCS
            blob = self.bucket.blob(filename)
            blob.upload_from_string(
                media_content,
                content_type=media_type,
            )

            # Generate URL
            url = blob.public_url

            return url, filename

        except base64.binascii.Error:
            raise ValueError("Invalid base64 media content")
        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to upload media: {str(e)}")

    def get_media(self, filename: str) -> Optional[str]:
        """
        Retrieve a media from the bucket and return it as base64.

        Args:
            filename: The filename of the media in the bucket

        Returns:
            Base64 encoded media string or None if not found

        Raises:
            Exception: If download fails
        """
        try:
            blob = self.bucket.blob(filename)
            media_content = blob.download_as_bytes()
            base64_media = base64.b64encode(media_content).decode("utf-8")

            return base64_media

        except exceptions.NotFound:
            return None
        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to retrieve media: {str(e)}")

    def delete_media(self, filename: str) -> bool:
        """
        Delete a media from the bucket.

        Args:
            filename: The filename of the media to delete

        Returns:
            Boolean indicating success

        Raises:
            Exception: If deletion fails
        """
        try:
            blob = self.bucket.blob(filename)
            blob.delete()
            return True

        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to delete media: {str(e)}")

    def get_media_url(self, filename: str) -> str:
        """
        Generate the URL for a media in the bucket.

        Args:
            filename: The filename of the media

        Returns:
            Full URL to the media
        """
        blob = self.bucket.blob(filename)
        return blob.public_url

    # -----------------------------------------------------------------
    #          Assistant media upload operations (assistant-centric)
    # -----------------------------------------------------------------

    def upload_assistant_media_file(
        self,
        file_content: bytes,
        content_type: str = "image/jpeg",
        *,
        assistant_id: str | int | None = None,
        user_id: str | None = None,
    ) -> str:
        """
        Upload a media file to the assistant media bucket.

        When ``assistant_id`` is provided the file is stored under the
        assistant-centric path ``{assistant_id}/{media_type}/{filename}``.

        When only ``user_id`` is supplied (legacy callers) the file is stored
        under ``{user_id}/{filename}`` for backward compatibility.

        Args:
            file_content: Raw bytes of the file.
            content_type: MIME type of the file (default ``image/jpeg``).
            assistant_id: Target assistant ID (preferred).
            user_id: Deprecated – kept for backward compatibility.

        Returns:
            The ``gs://`` URL of the uploaded object.

        Raises:
            ValueError: If neither *assistant_id* nor *user_id* is provided.
            Exception: If upload fails.
        """
        if not assistant_id and not user_id:
            raise ValueError(
                "Either assistant_id or user_id must be provided for upload path.",
            )

        try:
            extension = (
                content_type.split("/")[-1]
                if content_type and "/" in content_type
                else "jpg"
            )
            file_name = self._generate_unique_filename(file_content)

            if assistant_id:
                media_type = self._media_type_from_content_type(content_type)
                object_path = self.build_assistant_path(
                    assistant_id,
                    media_type,
                    f"{file_name}.{extension}",
                )
            else:
                # Legacy path: {user_id}/{filename}
                object_path = f"{user_id}/{file_name}.{extension}"

            blob = self.assistant_media_bucket.blob(object_path)
            blob.upload_from_string(file_content, content_type=content_type)
            gcs_url = f"gs://{self.assistant_media_bucket_name}/{object_path}"

            return gcs_url
        except exceptions.GoogleAPIError as e:
            logging.error(f"Failed to upload assistant media to GCS: {str(e)}")
            raise Exception(f"Failed to upload assistant media: {str(e)}")

    def upload_assistant_photo_file(
        self,
        file_content: bytes,
        user_id: str,
        content_type: str = "image/jpeg",
        *,
        assistant_id: str | int | None = None,
    ) -> str:
        """
        Upload an assistant's profile photo/video file.

        This is a backward-compatible wrapper around
        :meth:`upload_assistant_media_file`.  New callers should prefer
        ``upload_assistant_media_file(…, assistant_id=…)`` directly.
        """
        return self.upload_assistant_media_file(
            file_content,
            content_type,
            assistant_id=assistant_id,
            user_id=user_id,
        )

    # -----------------------------------------------------------------
    #                 User profile photo operations
    # -----------------------------------------------------------------

    def upload_user_photo_file(
        self,
        file_content: bytes,
        user_id: str,
        content_type: str = "image/jpeg",
    ) -> str:
        """
        Upload a user's profile photo to the account photo bucket.

        Stored under ``user/{user_id}/{filename}``.

        Args:
            file_content: Raw bytes of the image file.
            user_id: The user's ID.
            content_type: MIME type of the file (default ``image/jpeg``).

        Returns:
            The ``gs://`` URL of the uploaded object.
        """
        extension = (
            content_type.split("/")[-1]
            if content_type and "/" in content_type
            else "jpg"
        )
        file_name = self._generate_unique_filename(file_content)
        object_path = f"user/{user_id}/{file_name}.{extension}"

        blob = self.account_photo_bucket.blob(object_path)
        blob.upload_from_string(file_content, content_type=content_type)
        return f"gs://{self.account_photo_bucket_name}/{object_path}"

    def upload_org_photo_file(
        self,
        file_content: bytes,
        org_id: int,
        content_type: str = "image/jpeg",
    ) -> str:
        """
        Upload an organization's profile photo to the account photo bucket.

        Stored under ``organization/{org_id}/{filename}``.
        """
        extension = (
            content_type.split("/")[-1]
            if content_type and "/" in content_type
            else "jpg"
        )
        file_name = self._generate_unique_filename(file_content)
        object_path = f"organization/{org_id}/{file_name}.{extension}"

        blob = self.account_photo_bucket.blob(object_path)
        blob.upload_from_string(file_content, content_type=content_type)
        return f"gs://{self.account_photo_bucket_name}/{object_path}"

    # -----------------------------------------------------------------
    #                   Temporary file operations
    # -----------------------------------------------------------------

    def upload_temp_file(
        self,
        file_content: bytes,
        content_type: str,
    ) -> Tuple[str, str]:
        """
        Upload a temporary file to the root-level ``tmp/`` folder in the
        assistant media bucket and return a signed URL + GCS URI.

        Temp files are ephemeral and subject to lifecycle auto-cleanup.

        Args:
            file_content: Raw bytes of the file.
            content_type: MIME type of the file.

        Returns:
            A tuple of ``(signed_url, gcs_uri)`` for the temporary file.

        Raises:
            Exception: If upload or URL signing fails.
        """
        try:
            extension = (
                content_type.split("/")[-1]
                if content_type and "/" in content_type
                else "bin"
            )
            if "audio" in (content_type or ""):
                extension = (
                    content_type.split("/")[-1] if "/" in content_type else "wav"
                )

            file_name = self._generate_unique_filename(file_content)
            object_path = f"tmp/{file_name}.{extension}"

            blob = self.assistant_media_bucket.blob(object_path)
            blob.upload_from_string(file_content, content_type=content_type)

            expiration_timedelta = datetime.timedelta(hours=1)
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=expiration_timedelta,
                method="GET",
            )

            gcs_uri = f"gs://{self.assistant_media_bucket_name}/{object_path}"

            return signed_url, gcs_uri
        except exceptions.GoogleAPIError as e:
            logging.error(
                f"Failed to upload temporary file to GCS or sign URL: {str(e)}",
            )
            raise Exception(f"Failed to upload temporary file: {str(e)}")
        except Exception as e:
            logging.error(
                f"Unexpected error in upload_temp_file: {str(e)}",
            )
            raise Exception(f"Failed to process temporary file: {str(e)}")

    def upload_temp_assistant_file(
        self,
        file_content: bytes,
        user_id: str,
        content_type: str,
    ) -> Tuple[str, str]:
        """
        Backward-compatible wrapper around :meth:`upload_temp_file`.

        .. deprecated::
            The ``user_id`` parameter is no longer used for path construction.
            Temporary files are now stored in a root-level ``tmp/`` folder.
            Use :meth:`upload_temp_file` directly.
        """
        return self.upload_temp_file(
            file_content=file_content,
            content_type=content_type,
        )

    # -----------------------------------------------------------------
    #               File deletion (works with gs:// URLs)
    # -----------------------------------------------------------------

    def delete_assistant_file(self, gcs_url: str) -> bool:
        """
        Delete an assistant's file from GCS using its ``gs://`` URL.

        Ensures deletion only occurs from the designated assistant media bucket.

        Args:
            gcs_url: The GCS URL of the file (e.g., ``gs://bucket/path/to/file.jpg``).

        Returns:
            Boolean indicating success.

        Raises:
            Exception: If deletion fails for reasons other than NotFound.
        """
        parsed_bucket, object_path = parse_gcs_url(gcs_url)

        if not parsed_bucket or not object_path:
            logging.warning(f"Invalid GCS URL for deletion: {gcs_url}")
            return False

        if parsed_bucket != self.assistant_media_bucket_name:
            logging.error(
                f"Attempt to delete file from incorrect bucket. "
                f"Expected '{self.assistant_media_bucket_name}', got '{parsed_bucket}'. "
                f"URL: {gcs_url}",
            )
            return False
        try:
            blob = self.assistant_media_bucket.blob(object_path)
            blob.delete()
            logging.info(f"Successfully deleted assistant file: {gcs_url}")
            return True
        except exceptions.NotFound:
            logging.warning(
                f"Assistant file not found during deletion: {gcs_url}",
            )
            return True
        except exceptions.GoogleAPIError as e:
            logging.error(f"Failed to delete assistant file {gcs_url}: {str(e)}")
            raise Exception(f"Failed to delete assistant file: {str(e)}")

    # -----------------------------------------------------------------
    #                   Call recording operations
    # -----------------------------------------------------------------

    def delete_assistant_recordings(
        self,
        assistant_id: str | int,
        *,
        is_staging: bool = False,
    ) -> int:
        """
        Delete all call recordings for an assistant from GCS.

        Searches under both the new assistant-centric path
        (``{assistant_id}/``) and legacy environment-prefixed paths
        (``staging/{assistant_id}/``, ``production/{assistant_id}/``) to
        ensure all recordings are cleaned up during migration.

        Returns:
            Number of successfully deleted files.
        """
        # New path + legacy paths for backward compat
        prefixes = [
            f"{assistant_id}/",
            f"staging/{assistant_id}/",
            f"production/{assistant_id}/",
        ]
        deleted_count = 0

        for prefix in prefixes:
            try:
                blobs = self.call_recordings_bucket.list_blobs(prefix=prefix)
                for blob in blobs:
                    try:
                        blob.delete()
                        deleted_count += 1
                        logging.debug(f"Deleted recording: {blob.name}")
                    except exceptions.GoogleAPIError as e:
                        logging.error(
                            f"Failed to delete recording {blob.name}: {str(e)}",
                        )
            except exceptions.GoogleAPIError as e:
                logging.error(
                    f"Failed to list recordings under {prefix}: {str(e)}",
                )

        if deleted_count > 0:
            logging.info(
                f"Deleted {deleted_count} recording(s) for assistant {assistant_id}",
            )
        else:
            logging.debug(f"No recordings found for assistant {assistant_id}")

        return deleted_count

    # -----------------------------------------------------------------
    #                Message attachment operations
    # -----------------------------------------------------------------

    def delete_assistant_attachments(self, assistant_id: str | int) -> int:
        """
        Delete all message attachments for an assistant from GCS.

        Attachments are stored with assistant-scoped paths:
        ``{assistant_id}/{attachment_id}_{filename}``

        Args:
            assistant_id: The assistant's ID.

        Returns:
            Number of successfully deleted files.
        """
        prefix = f"{assistant_id}/"
        deleted_count = 0

        try:
            blobs = self.message_attachments_bucket.list_blobs(prefix=prefix)

            for blob in blobs:
                try:
                    blob.delete()
                    deleted_count += 1
                    logging.debug(f"Deleted attachment: {blob.name}")
                except exceptions.GoogleAPIError as e:
                    logging.error(f"Failed to delete attachment {blob.name}: {str(e)}")

            if deleted_count > 0:
                logging.info(
                    f"Deleted {deleted_count} message attachment(s) for assistant {assistant_id}",
                )
            else:
                logging.debug(
                    f"No message attachments found for assistant {assistant_id}",
                )

            return deleted_count

        except exceptions.GoogleAPIError as e:
            logging.error(
                f"Failed to list attachments for assistant {assistant_id}: {str(e)}",
            )
            return deleted_count

    def delete_message_attachments_for_user(self, user_id: str | int) -> int:
        """
        Delete all message attachments for a user from GCS.

        .. deprecated::
            Use :meth:`delete_assistant_attachments` for assistant-centric
            cleanup.  This method is retained for the user-account-deletion
            flow which still iterates by user_id during the migration period.

        Searches under the legacy ``{user_id}/`` prefix.

        Args:
            user_id: The user's ID (can be string or int).

        Returns:
            Number of successfully deleted files.
        """
        prefix = f"{user_id}/"
        deleted_count = 0

        try:
            blobs = self.message_attachments_bucket.list_blobs(prefix=prefix)

            for blob in blobs:
                try:
                    blob.delete()
                    deleted_count += 1
                    logging.debug(f"Deleted attachment: {blob.name}")
                except exceptions.GoogleAPIError as e:
                    logging.error(f"Failed to delete attachment {blob.name}: {str(e)}")

            if deleted_count > 0:
                logging.info(
                    f"Deleted {deleted_count} message attachments for user {user_id}",
                )
            else:
                logging.debug(f"No message attachments found for user {user_id}")

            return deleted_count

        except exceptions.GoogleAPIError as e:
            logging.error(
                f"Failed to list attachments for user {user_id}: {str(e)}",
            )
            return deleted_count

    # -----------------------------------------------------------------
    #          Full assistant cleanup (all buckets at once)
    # -----------------------------------------------------------------

    def delete_all_assistant_data(
        self,
        assistant_id: str | int,
        *,
        is_staging: bool = False,
    ) -> dict:
        """
        Delete **all** GCS data for an assistant across every bucket.

        Convenience method used during assistant deletion to ensure nothing
        is left behind.  Calls the individual delete helpers and returns a
        summary dict.

        Args:
            assistant_id: The assistant's ID.
            is_staging: Whether this is a staging environment.

        Returns:
            Dict with counts per bucket, e.g.
            ``{"media": 3, "recordings": 1, "attachments": 5}``.
        """
        media_count = 0
        prefix = f"{assistant_id}/"

        # --- assistant media bucket ---
        try:
            blobs = self.assistant_media_bucket.list_blobs(prefix=prefix)
            for blob in blobs:
                try:
                    blob.delete()
                    media_count += 1
                except exceptions.GoogleAPIError as e:
                    logging.error(
                        f"Failed to delete media {blob.name}: {str(e)}",
                    )
        except exceptions.GoogleAPIError as e:
            logging.error(
                f"Failed to list media under {prefix}: {str(e)}",
            )

        # --- recordings ---
        recordings_count = self.delete_assistant_recordings(
            assistant_id,
            is_staging=is_staging,
        )

        # --- attachments ---
        attachments_count = self.delete_assistant_attachments(assistant_id)

        total = media_count + recordings_count + attachments_count
        if total > 0:
            logging.info(
                f"Cleaned up {total} file(s) for assistant {assistant_id} "
                f"(media={media_count}, recordings={recordings_count}, "
                f"attachments={attachments_count})",
            )

        return {
            "media": media_count,
            "recordings": recordings_count,
            "attachments": attachments_count,
        }

    # -----------------------------------------------------------------
    #          Account photo cleanup (user / organization)
    # -----------------------------------------------------------------

    def delete_user_account_photos(self, user_id: str) -> int:
        """
        Delete **all** account photos for a user from the account photo bucket.

        Removes every object under ``user/{user_id}/``.

        Args:
            user_id: The user's ID.

        Returns:
            The number of files deleted.
        """
        return self._delete_account_photo_prefix(f"user/{user_id}/")

    def delete_org_account_photos(self, org_id: int) -> int:
        """
        Delete **all** account photos for an organization from the account
        photo bucket.

        Removes every object under ``organization/{org_id}/``.

        Args:
            org_id: The organization's ID.

        Returns:
            The number of files deleted.
        """
        return self._delete_account_photo_prefix(f"organization/{org_id}/")

    def _delete_account_photo_prefix(self, prefix: str) -> int:
        """Delete all objects under *prefix* in the account photo bucket."""
        deleted = 0
        try:
            blobs = self.account_photo_bucket.list_blobs(prefix=prefix)
            for blob in blobs:
                try:
                    blob.delete()
                    deleted += 1
                    logging.debug(f"Deleted account photo: {blob.name}")
                except exceptions.GoogleAPIError as e:
                    logging.error(
                        f"Failed to delete account photo {blob.name}: {e}",
                    )
            if deleted > 0:
                logging.info(
                    f"Deleted {deleted} account photo(s) under "
                    f"{self.account_photo_bucket_name}/{prefix}",
                )
        except exceptions.GoogleAPIError as e:
            logging.error(
                f"Failed to list account photos under {prefix}: {e}",
            )
        return deleted
