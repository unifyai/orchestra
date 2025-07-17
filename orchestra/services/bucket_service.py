import base64
import datetime
import hashlib
import logging
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

    def _generate_unique_filename(self, content: bytes) -> str:
        """Generate a unique filename using content hash and UUID."""
        content_hash = hashlib.md5(content).hexdigest()
        unique_id = str(uuid.uuid4())[:8]
        return f"{content_hash}_{unique_id}"

    def upload_image(self, base64_image: str) -> Tuple[str, str]:
        """
        Upload a base64 encoded image to the bucket.

        Args:
            base64_image: Base64 encoded image string

        Returns:
            Tuple containing the image URL and filename

        Raises:
            ValueError: If the base64 image is invalid
            Exception: If upload fails
        """
        try:
            # Remove potential base64 prefix
            if "," in base64_image:
                base64_image = base64_image.split(",")[1]

            # Decode base64 image
            image_content = base64.b64decode(base64_image)

            # Generate unique filename
            filename = self._generate_unique_filename(image_content)

            # Upload to GCS
            blob = self.bucket.blob(filename)
            blob.upload_from_string(
                image_content,
                content_type="image/jpeg",  # Adjust content type as needed
            )

            # Generate URL
            url = blob.public_url

            return url, filename

        except base64.binascii.Error:
            raise ValueError("Invalid base64 image content")
        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to upload image: {str(e)}")

    def upload_audio(self, base64_audio: str) -> Tuple[str, str]:
        """
        Upload a base64 encoded audio file to the bucket.

        Args:
            base64_audio: Base64 encoded audio string

        Returns:
            Tuple containing the audio URL and filename

        Raises:
            ValueError: If the base64 audio is invalid
            Exception: If upload fails
        """
        try:
            # Remove potential base64 prefix if present
            if "," in base64_audio:
                base64_audio = base64_audio.split(",")[1]

            # Decode base64 audio
            audio_content = base64.b64decode(base64_audio)

            # Generate unique filename
            filename = self._generate_unique_filename(audio_content)

            # Upload to GCS
            blob = self.bucket.blob(filename)
            # A more generic content type, or one inferred from the base64 prefix if available
            blob.upload_from_string(
                audio_content,
                content_type="audio/wav",  # Assume WAV for now, could be made more sophisticated
            )

            # Generate URL
            url = blob.public_url

            return url, filename

        except base64.binascii.Error:
            raise ValueError("Invalid base64 audio content")
        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to upload audio: {str(e)}")

    def upload_recording(self, content: bytes, content_type: str) -> Tuple[str, str]:
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
            filename = self._generate_unique_filename(content)

            # Upload to GCS
            blob = self.bucket.blob(filename)
            blob.upload_from_string(content, content_type=content_type)

            # Generate URL
            url = blob.public_url

            return url, filename

        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to upload recording: {str(e)}")

    def get_image(self, filename: str) -> Optional[str]:
        """
        Retrieve an image from the bucket and return it as base64.

        Args:
            filename: The filename of the image in the bucket

        Returns:
            Base64 encoded image string or None if not found

        Raises:
            Exception: If download fails
        """
        try:
            blob = self.bucket.blob(filename)
            image_content = blob.download_as_bytes()
            base64_image = base64.b64encode(image_content).decode("utf-8")

            return base64_image

        except exceptions.NotFound:
            return None
        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to retrieve image: {str(e)}")

    def get_audio(self, filename: str) -> Optional[str]:
        """
        Retrieve an audio file from the bucket and return it as base64.

        Args:
            filename: The filename of the audio in the bucket

        Returns:
            Base64 encoded audio string or None if not found

        Raises:
            Exception: If download fails
        """
        try:
            blob = self.bucket.blob(filename)
            audio_content = blob.download_as_bytes()
            base64_audio = base64.b64encode(audio_content).decode("utf-8")

            return base64_audio

        except exceptions.NotFound:
            return None
        except exceptions.GoogleAPIError as e:
            raise Exception(f"Failed to retrieve audio: {str(e)}")

    def delete_image(self, filename: str) -> bool:
        """
        Delete an image from the bucket.

        Args:
            filename: The filename of the image to delete

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
            raise Exception(f"Failed to delete image: {str(e)}")

    def get_image_url(self, filename: str) -> str:
        """
        Generate the URL for an image in the bucket.

        Args:
            filename: The filename of the image

        Returns:
            Full URL to the image
        """
        blob = self.bucket.blob(filename)
        return blob.public_url

    # -- Assistant photos --
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
