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

from orchestra.web.api.utils.gcp import parse_gcs_url


class BucketService:
    def __init__(self):
        """Initialize the bucket service with GCP credentials and bucket configuration."""

        self.credentials_path = os.getenv("ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON")
        if not self.credentials_path:
            raise ValueError(
                "Missing required GCP credentials key (ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON)",
            )

        self.project_id = os.getenv("ORCHESTRA_VERTEXAI_PROJECT")
        if not self.project_id:
            raise ValueError(
                "Missing required GCP project ID configuration (ORCHESTRA_VERTEXAI_PROJECT)",
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

        # Assistant images bucket
        self.assistant_images_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_IMAGES_BUCKET_NAME",
        )
        if not self.assistant_images_bucket_name:
            raise ValueError(
                "Missing required GCP assistant images bucket name configuration (ORCHESTRA_GCP_ASSISTANT_IMAGES_BUCKET_NAME)",
            )
        self.assistant_images_bucket = self.storage_client.bucket(
            self.assistant_images_bucket_name,
        )

    def _generate_unique_filename(self, content: bytes, extension: str = "") -> str:
        """Generate a unique filename using content hash, UUID, and an optional extension."""
        content_hash = hashlib.md5(content).hexdigest()
        unique_id = str(uuid.uuid4())[:8]
        if extension:
            return f"{content_hash}_{unique_id}.{extension.lstrip('.')}"
        return f"{content_hash}_{unique_id}"

    def upload_recording(
        self,
        content: bytes,
        content_type: str,
        is_staging: bool = False,
    ) -> Tuple[str, str]:
        """
        Upload raw audio bytes to GCS and return (url, filename).

        Args:
            content: Raw audio bytes to upload
            content_type: MIME type of the audio content (e.g., 'audio/wav', 'audio/mp3')

        Returns:
            Tuple containing the recording URL and filename

        Raises:
            Exception: If upload fails
        """
        try:
            # Generate unique filename
            unique_filename = self._generate_unique_filename(content)
            if is_staging:
                filename = "assistant_call_recording_staging/" + unique_filename
            else:
                filename = "assistant_call_recording/" + unique_filename

            # Upload to GCS
            blob = self.bucket.blob(filename)
            blob.upload_from_string(content, content_type=content_type)

            # Generate URL
            url = blob.public_url

            return url, filename

        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to upload recording: {str(e)}")

    # -------------------------------------------------------------
    #                   General media operations
    # -------------------------------------------------------------
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

    # -------------------------------------------------------------
    #            Assistant photo and temp file operations
    # -------------------------------------------------------------
    def upload_assistant_photo_file(
        self,
        file_content: bytes,
        user_id: str,  # For path organization
        content_type: str = "image/jpeg",  # Default, can be overridden
    ) -> str:
        """
        Uploads an assistant's profile photo file to the assistant images GCS bucket.
        Args:
            file_content: Raw bytes of the image file.
            user_id: ID of the user uploading the photo, for path organization.
            content_type: MIME type of the image.
        Returns:
            The GCS URL (gs://bucket-name/object-path) of the uploaded photo.
        Raises:
            Exception: If upload fails.
        """
        try:
            extension = (
                content_type.split("/")[-1]
                if content_type and "/" in content_type
                else "jpg"
            )
            file_name = self._generate_unique_filename(file_content)
            object_path = f"{user_id}/{file_name}.{extension}"

            blob = self.assistant_images_bucket.blob(object_path)
            blob.upload_from_string(file_content, content_type=content_type)
            gcs_url = f"gs://{self.assistant_images_bucket_name}/{object_path}"

            return gcs_url
        except exceptions.GoogleAPIError as e:
            logging.error(f"Failed to upload assistant photo to GCS: {str(e)}")
            raise Exception(f"Failed to upload assistant photo: {str(e)}")

    def upload_temp_assistant_file(
        self,
        file_content: bytes,
        user_id: str,
        content_type: str,
    ) -> Tuple[str, str]:
        """
        Uploads a temporary photo/file to a '_temp' subfolder in the assistant images bucket
        and returns a signed URL for public access, along with its GCS URI.
        Args:
            file_content: Raw bytes of the file.
            user_id: ID of the user, for path organization.
            content_type: MIME type of the file.
        Returns:
            A tuple of (publicly_accessible_signed_url, gcs_uri) for the temporary file.
        Raises:
            Exception: If upload or URL signing fails.
        """
        try:
            extension = (
                content_type.split("/")[-1]
                if content_type and "/" in content_type
                else "jpg"
            )
            if "audio" in content_type:
                extension = (
                    content_type.split("/")[-1] if "/" in content_type else "wav"
                )

            file_name = self._generate_unique_filename(file_content)
            object_path = f"_temp/{user_id}/{file_name}.{extension}"

            blob = self.assistant_images_bucket.blob(object_path)
            blob.upload_from_string(file_content, content_type=content_type)

            expiration_timedelta = datetime.timedelta(hours=1)  # URL valid for 1 hour
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=expiration_timedelta,
                method="GET",
            )

            gcs_uri = f"gs://{self.assistant_images_bucket_name}/{object_path}"

            return signed_url, gcs_uri
        except exceptions.GoogleAPIError as e:
            logging.error(
                f"Failed to upload temporary file to GCS or sign URL: {str(e)}",
            )
            raise Exception(f"Failed to upload temporary file: {str(e)}")
        except Exception as e:
            logging.error(
                f"Unexpected error in upload_temp_assistant_file: {str(e)}",
            )
            raise Exception(f"Failed to process temporary file: {str(e)}")

    def delete_assistant_file(self, gcs_url: str) -> bool:
        """
        Delete an assistant's profile photo/file from GCS using its GCS URL.
        Ensures deletion only occurs from the designated assistant images bucket.
        Args:
            gcs_url: The GCS URL of the photo/file to delete (e.g., gs://bucket/path/to/photo.jpg)
        Returns:
            Boolean indicating success.
        Raises:
            Exception: If deletion fails for reasons other than NotFound.
        """
        parsed_bucket, object_path = parse_gcs_url(gcs_url)

        if not parsed_bucket or not object_path:
            logging.warning(f"Invalid GCS URL for deletion: {gcs_url}")
            return False

        if parsed_bucket != self.assistant_images_bucket_name:
            logging.error(
                f"Attempt to delete photo/file from incorrect bucket. Expected '{self.assistant_images_bucket_name}', got '{parsed_bucket}'. URL: {gcs_url}",
            )
            return False
        try:
            blob = self.assistant_images_bucket.blob(object_path)
            blob.delete()
            logging.info(f"Successfully deleted assistant photo/file: {gcs_url}")
            return True
        except exceptions.NotFound:
            logging.warning(
                f"Assistant photo/file not found during deletion: {gcs_url}",
            )
            return True
        except exceptions.GoogleAPIError as e:
            logging.error(f"Failed to delete assistant photo/file {gcs_url}: {str(e)}")
            raise Exception(f"Failed to delete assistant photo/file: {str(e)}")
