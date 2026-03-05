"""
Tests for account photo storage and cleanup functionality.

These tests verify:
- BucketService account photo bucket naming and configuration
- User and org photo uploads use the dedicated account photo bucket
- Photo cleanup methods delete the correct GCS prefixes
- GCS URL parsing works for account photo bucket URIs
- User deletion includes account photo cleanup
- Org deletion includes account photo cleanup
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import GoogleAPIError

# =============================================================================
# BucketService - Account Photo Bucket Configuration
# =============================================================================


class TestAccountPhotoBucketNaming:
    """Tests for account photo bucket naming and configuration."""

    def test_account_photo_bucket_from_env_var(self):
        """Account photo bucket uses ORCHESTRA_GCP_ACCOUNT_PHOTO_BUCKET_NAME."""
        from orchestra.services.bucket_service import BucketService

        with patch.dict(
            os.environ,
            {
                "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
                "GCP_PROJECT_ID": "test-project",
                "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
                "ORCHESTRA_GCP_ACCOUNT_PHOTO_BUCKET_NAME": "my-custom-photo-bucket",
            },
        ):
            with patch("orchestra.services.bucket_service.service_account"):
                with patch(
                    "orchestra.services.bucket_service.storage.Client",
                ) as mock_client:
                    mock_client.return_value = MagicMock()
                    service = BucketService()

                    assert service.account_photo_bucket_name == "my-custom-photo-bucket"
                    assert service.account_photo_bucket is not None

    def test_account_photo_bucket_defaults_to_staging(self):
        """Without env var, staging environment defaults to account-photo-staging."""
        from orchestra.services.bucket_service import BucketService

        env = {
            "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
            "GCP_PROJECT_ID": "test-project",
            "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
        }
        # Remove the env var if set
        env_to_remove = ["ORCHESTRA_GCP_ACCOUNT_PHOTO_BUCKET_NAME"]

        with patch.dict(os.environ, env, clear=False):
            for key in env_to_remove:
                os.environ.pop(key, None)

            with patch("orchestra.services.bucket_service.service_account"):
                with patch(
                    "orchestra.services.bucket_service.storage.Client",
                ) as mock_client:
                    mock_client.return_value = MagicMock()
                    with patch(
                        "orchestra.settings.settings",
                    ) as mock_settings:
                        mock_settings.is_staging = True
                        service = BucketService()

                        assert (
                            service.account_photo_bucket_name == "account-photo-staging"
                        )

    def test_account_photo_bucket_defaults_to_production(self):
        """Without env var, production environment defaults to account-photo-production."""
        from orchestra.services.bucket_service import BucketService

        env = {
            "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
            "GCP_PROJECT_ID": "test-project",
            "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
        }
        env_to_remove = ["ORCHESTRA_GCP_ACCOUNT_PHOTO_BUCKET_NAME"]

        with patch.dict(os.environ, env, clear=False):
            for key in env_to_remove:
                os.environ.pop(key, None)

            with patch("orchestra.services.bucket_service.service_account"):
                with patch(
                    "orchestra.services.bucket_service.storage.Client",
                ) as mock_client:
                    mock_client.return_value = MagicMock()
                    with patch(
                        "orchestra.settings.settings",
                    ) as mock_settings:
                        mock_settings.is_staging = False
                        service = BucketService()

                        assert (
                            service.account_photo_bucket_name
                            == "account-photo-production"
                        )


# =============================================================================
# BucketService - User Photo Upload
# =============================================================================


class TestUploadUserPhotoFile:
    """Tests for upload_user_photo_file using the account photo bucket."""

    @pytest.fixture
    def bucket_service(self):
        from orchestra.services.bucket_service import BucketService

        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_blob.upload_from_string.return_value = None

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.account_photo_bucket = mock_bucket
            service.account_photo_bucket_name = "account-photo-staging"
            yield service, mock_bucket, mock_blob

    def test_uploads_to_user_subfolder(self, bucket_service):
        """Photo is stored under user/{user_id}/."""
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_user_photo_file(
            b"image-data",
            user_id="user-abc-123",
            content_type="image/jpeg",
        )

        blob_path = mock_bucket.blob.call_args[0][0]
        assert blob_path.startswith("user/user-abc-123/")
        assert blob_path.endswith(".jpeg")
        assert gcs_url.startswith("gs://account-photo-staging/user/user-abc-123/")

    def test_uploads_png(self, bucket_service):
        """PNG content type produces .png extension."""
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_user_photo_file(
            b"png-data",
            user_id="user-456",
            content_type="image/png",
        )

        blob_path = mock_bucket.blob.call_args[0][0]
        assert blob_path.endswith(".png")
        assert "user/user-456/" in gcs_url

    def test_uploads_webp(self, bucket_service):
        """WebP content type produces .webp extension."""
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_user_photo_file(
            b"webp-data",
            user_id="user-789",
            content_type="image/webp",
        )

        blob_path = mock_bucket.blob.call_args[0][0]
        assert blob_path.endswith(".webp")

    def test_default_content_type_is_jpeg(self, bucket_service):
        """Default content type is image/jpeg."""
        service, mock_bucket, mock_blob = bucket_service
        service.upload_user_photo_file(b"data", user_id="user-x")

        mock_blob.upload_from_string.assert_called_once_with(
            b"data",
            content_type="image/jpeg",
        )

    def test_returns_gs_url(self, bucket_service):
        """Returns a gs:// URL pointing to the account photo bucket."""
        service, _, _ = bucket_service
        gcs_url = service.upload_user_photo_file(b"data", user_id="u1")
        assert gcs_url.startswith("gs://account-photo-staging/user/u1/")


# =============================================================================
# BucketService - Org Photo Upload
# =============================================================================


class TestUploadOrgPhotoFile:
    """Tests for upload_org_photo_file using the account photo bucket."""

    @pytest.fixture
    def bucket_service(self):
        from orchestra.services.bucket_service import BucketService

        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_blob.upload_from_string.return_value = None

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.account_photo_bucket = mock_bucket
            service.account_photo_bucket_name = "account-photo-production"
            yield service, mock_bucket, mock_blob

    def test_uploads_to_organization_subfolder(self, bucket_service):
        """Photo is stored under organization/{org_id}/."""
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_org_photo_file(
            b"image-data",
            org_id=42,
            content_type="image/png",
        )

        blob_path = mock_bucket.blob.call_args[0][0]
        assert blob_path.startswith("organization/42/")
        assert blob_path.endswith(".png")
        assert gcs_url.startswith("gs://account-photo-production/organization/42/")

    def test_default_content_type_is_jpeg(self, bucket_service):
        """Default content type is image/jpeg."""
        service, _, mock_blob = bucket_service
        service.upload_org_photo_file(b"data", org_id=99)

        mock_blob.upload_from_string.assert_called_once_with(
            b"data",
            content_type="image/jpeg",
        )

    def test_returns_gs_url(self, bucket_service):
        """Returns a gs:// URL pointing to the account photo bucket."""
        service, _, _ = bucket_service
        gcs_url = service.upload_org_photo_file(b"data", org_id=7)
        assert gcs_url.startswith("gs://account-photo-production/organization/7/")


# =============================================================================
# BucketService - Account Photo Cleanup
# =============================================================================


class TestDeleteUserAccountPhotos:
    """Tests for delete_user_account_photos."""

    @pytest.fixture
    def bucket_service(self):
        from orchestra.services.bucket_service import BucketService

        mock_blob1 = MagicMock()
        mock_blob1.name = "user/user-123/abc.jpg"
        mock_blob1.delete = MagicMock()

        mock_blob2 = MagicMock()
        mock_blob2.name = "user/user-123/def.png"
        mock_blob2.delete = MagicMock()

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [mock_blob1, mock_blob2]

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.account_photo_bucket = mock_bucket
            service.account_photo_bucket_name = "account-photo-staging"
            yield service, mock_bucket, [mock_blob1, mock_blob2]

    def test_deletes_all_user_photos(self, bucket_service):
        """Deletes all objects under user/{user_id}/."""
        service, mock_bucket, blobs = bucket_service
        count = service.delete_user_account_photos("user-123")

        assert count == 2
        mock_bucket.list_blobs.assert_called_once_with(prefix="user/user-123/")
        for blob in blobs:
            blob.delete.assert_called_once()

    def test_returns_zero_when_no_photos(self, bucket_service):
        """Returns 0 when user has no photos."""
        service, mock_bucket, _ = bucket_service
        mock_bucket.list_blobs.return_value = []

        count = service.delete_user_account_photos("user-999")
        assert count == 0

    def test_handles_individual_deletion_errors(self, bucket_service):
        """Continues deleting even if one blob.delete() fails."""
        service, _, blobs = bucket_service
        blobs[0].delete.side_effect = GoogleAPIError("Failed")

        count = service.delete_user_account_photos("user-123")
        assert count == 1  # Only second blob succeeded
        blobs[1].delete.assert_called_once()

    def test_handles_list_blobs_error(self):
        """Returns 0 when list_blobs raises."""
        from orchestra.services.bucket_service import BucketService

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.side_effect = GoogleAPIError("List failed")

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.account_photo_bucket = mock_bucket
            service.account_photo_bucket_name = "account-photo-staging"

            count = service.delete_user_account_photos("user-x")
            assert count == 0


class TestDeleteOrgAccountPhotos:
    """Tests for delete_org_account_photos."""

    @pytest.fixture
    def bucket_service(self):
        from orchestra.services.bucket_service import BucketService

        mock_blob = MagicMock()
        mock_blob.name = "organization/42/logo.png"
        mock_blob.delete = MagicMock()

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [mock_blob]

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.account_photo_bucket = mock_bucket
            service.account_photo_bucket_name = "account-photo-production"
            yield service, mock_bucket, [mock_blob]

    def test_deletes_all_org_photos(self, bucket_service):
        """Deletes all objects under organization/{org_id}/."""
        service, mock_bucket, blobs = bucket_service
        count = service.delete_org_account_photos(42)

        assert count == 1
        mock_bucket.list_blobs.assert_called_once_with(prefix="organization/42/")
        blobs[0].delete.assert_called_once()

    def test_returns_zero_when_no_photos(self, bucket_service):
        """Returns 0 when org has no photos."""
        service, mock_bucket, _ = bucket_service
        mock_bucket.list_blobs.return_value = []

        count = service.delete_org_account_photos(99)
        assert count == 0


# =============================================================================
# GCS URL Parsing for Account Photo Bucket
# =============================================================================


class TestAccountPhotoGcsUrlParsing:
    """Verify parse_gcs_url handles account photo bucket URIs."""

    def test_parse_user_photo_url(self):
        from orchestra.web.api.utils.gcp import parse_gcs_url

        bucket, path = parse_gcs_url(
            "gs://account-photo-staging/user/user-abc/photo.jpg",
        )
        assert bucket == "account-photo-staging"
        assert path == "user/user-abc/photo.jpg"

    def test_parse_org_photo_url(self):
        from orchestra.web.api.utils.gcp import parse_gcs_url

        bucket, path = parse_gcs_url(
            "gs://account-photo-production/organization/42/logo.png",
        )
        assert bucket == "account-photo-production"
        assert path == "organization/42/logo.png"
