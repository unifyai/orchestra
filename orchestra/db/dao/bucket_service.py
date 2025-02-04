import base64
import hashlib
import os
import uuid
from typing import Optional, Tuple

from google.api_core import exceptions
from google.cloud import storage


class BucketService:
    def __init__(self):
        """Initialize the bucket service with GCP credentials and bucket configuration."""
        self.bucket_name = os.getenv("ORCHESTRA_GCP_BUCKET_NAME")
        self.project_id = os.getenv("ORCHESTRA_VERTEXAI_PROJECT")

        if not all([self.bucket_name, self.project_id]):
            raise ValueError("Missing required GCP configuration")

        # GCP client will automatically look for GOOGLE_APPLICATION_CREDENTIALS environment variable
        self.storage_client = storage.Client(project=self.project_id)
        self.bucket = self.storage_client.bucket(self.bucket_name)

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
