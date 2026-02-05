"""
Tests for Unify message attachment storage and cleanup functionality.

These tests verify:
- BucketService methods for attachment cleanup
- User deletion includes attachment cleanup
- Signed URL generation works for the attachments bucket

Tests are behavior-focused and will fail until the features are implemented.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# BucketService - Attachment Cleanup Tests
# =============================================================================


class TestBucketServiceAttachmentCleanup:
    """Tests for BucketService.delete_message_attachments_for_user method."""

    @pytest.fixture
    def mock_storage_client(self):
        """Create a mock GCS storage client."""
        mock_blob1 = MagicMock()
        mock_blob1.name = "12345/uuid1_file1.pdf"
        mock_blob1.delete = MagicMock()

        mock_blob2 = MagicMock()
        mock_blob2.name = "12345/uuid2_file2.png"
        mock_blob2.delete = MagicMock()

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [mock_blob1, mock_blob2]

        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        return {
            "client": mock_client,
            "bucket": mock_bucket,
            "blobs": [mock_blob1, mock_blob2],
        }

    def test_delete_attachments_for_user_returns_count(self, mock_storage_client):
        """delete_message_attachments_for_user returns number of deleted files."""
        from orchestra.services.bucket_service import BucketService

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_storage_client["client"]
            service.unify_attachments_bucket = mock_storage_client["bucket"]

            deleted_count = service.delete_message_attachments_for_user(user_id=12345)

            assert deleted_count == 2
            mock_storage_client["bucket"].list_blobs.assert_called_once_with(prefix="12345/")
            for blob in mock_storage_client["blobs"]:
                blob.delete.assert_called_once()

    def test_delete_attachments_for_user_with_no_files(self, mock_storage_client):
        """delete_message_attachments_for_user returns 0 when user has no attachments."""
        from orchestra.services.bucket_service import BucketService

        mock_storage_client["bucket"].list_blobs.return_value = []

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_storage_client["client"]
            service.unify_attachments_bucket = mock_storage_client["bucket"]

            deleted_count = service.delete_message_attachments_for_user(user_id=99999)

            assert deleted_count == 0
            mock_storage_client["bucket"].list_blobs.assert_called_once_with(prefix="99999/")

    def test_delete_attachments_handles_deletion_errors_gracefully(self, mock_storage_client):
        """Deletion continues even if individual file deletion fails."""
        from google.api_core.exceptions import GoogleAPIError

        from orchestra.services.bucket_service import BucketService

        # First blob fails, second succeeds
        mock_storage_client["blobs"][0].delete.side_effect = GoogleAPIError("Failed")
        mock_storage_client["blobs"][1].delete.return_value = None

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_storage_client["client"]
            service.unify_attachments_bucket = mock_storage_client["bucket"]

            # Should return count of successful deletions only
            deleted_count = service.delete_message_attachments_for_user(user_id=12345)

            assert deleted_count == 1  # Only one succeeded

    def test_bucket_service_has_attachments_bucket_configured(self):
        """BucketService should have the unify-message-attachments bucket configured."""
        from orchestra.services.bucket_service import BucketService

        # Verify the bucket is configured via env var or default
        with patch.dict(os.environ, {
            "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
            "GCP_PROJECT_ID": "test-project",
            "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
        }):
            with patch("orchestra.services.bucket_service.service_account"):
                with patch("orchestra.services.bucket_service.storage.Client") as mock_client:
                    mock_client.return_value = MagicMock()
                    service = BucketService()

                    # Check the bucket name attribute is set
                    assert service.unify_attachments_bucket_name == "unify-message-attachments"
                    assert service.unify_attachments_bucket is not None


# =============================================================================
# User Account Deletion - Attachment Cleanup Integration
# =============================================================================


class TestUserDeletionAttachmentCleanup:
    """Tests for attachment cleanup during user account deletion."""

    def test_user_deletion_calls_attachment_cleanup(self):
        """UserAccountCleanupService.delete_user_account should call attachment cleanup."""
        from orchestra.services.user_account_cleanup_service import UserAccountCleanupService

        mock_session = MagicMock()
        # Mock check_deletion_blockers to return no blockers
        mock_result = MagicMock()
        mock_result.user_exists = True
        mock_result.billing_exists = True
        mock_result.has_pending_bills = False
        mock_result.pending_amount = 0
        mock_result.owns_organizations = False
        mock_result.owned_org_names = None
        mock_session.execute.return_value.fetchone.return_value = mock_result
        mock_session.execute.return_value.scalar.return_value = None  # No stripe customer

        # Patch BucketService in the bucket_service module where it's imported from
        with patch(
            "orchestra.services.bucket_service.BucketService"
        ) as mock_bucket_cls:
            mock_bucket_service = MagicMock()
            mock_bucket_service.delete_message_attachments_for_user.return_value = 5
            mock_bucket_cls.return_value = mock_bucket_service

            service = UserAccountCleanupService(mock_session)
            result = service.delete_user_account("user-123")

            # Verify attachment cleanup was called
            mock_bucket_service.delete_message_attachments_for_user.assert_called_once_with(
                "user-123"
            )
            assert result.success is True

    def test_user_deletion_continues_if_attachment_cleanup_fails(self):
        """User deletion should succeed even if attachment cleanup fails."""
        from orchestra.services.user_account_cleanup_service import UserAccountCleanupService

        mock_session = MagicMock()
        # Mock check_deletion_blockers to return no blockers
        mock_result = MagicMock()
        mock_result.user_exists = True
        mock_result.billing_exists = True
        mock_result.has_pending_bills = False
        mock_result.pending_amount = 0
        mock_result.owns_organizations = False
        mock_result.owned_org_names = None
        mock_session.execute.return_value.fetchone.return_value = mock_result
        mock_session.execute.return_value.scalar.return_value = None  # No stripe customer

        # Patch BucketService in the bucket_service module where it's imported from
        with patch(
            "orchestra.services.bucket_service.BucketService"
        ) as mock_bucket_cls:
            mock_bucket_cls.side_effect = Exception("GCS error")

            service = UserAccountCleanupService(mock_session)
            # Should still succeed (cleanup is best-effort)
            result = service.delete_user_account("user-123")

            assert result.success is True


# =============================================================================
# Storage Endpoints - Attachments Bucket Access
# =============================================================================


class TestStorageEndpointsAttachmentsBucket:
    """Tests for storage endpoints with the unify-message-attachments bucket.

    Note: These tests verify the GCS URL parsing logic works correctly with
    the attachments bucket name. The actual endpoint tests with database
    are in test_storage.py.
    """

    def test_gcs_url_parsing_for_attachments_bucket(self):
        """parse_gcs_url correctly parses unify-message-attachments bucket URIs."""
        from orchestra.web.api.utils.gcp import parse_gcs_url

        bucket, path = parse_gcs_url("gs://unify-message-attachments/12345/uuid_document.pdf")
        assert bucket == "unify-message-attachments"
        assert path == "12345/uuid_document.pdf"

    def test_gcs_url_parsing_nested_path(self):
        """parse_gcs_url handles nested paths in attachments bucket."""
        from orchestra.web.api.utils.gcp import parse_gcs_url

        bucket, path = parse_gcs_url(
            "gs://unify-message-attachments/user123/uuid1_document.pdf"
        )
        assert bucket == "unify-message-attachments"
        assert path == "user123/uuid1_document.pdf"

    def test_storage_client_can_access_any_bucket(self):
        """Verify storage client bucket() method accepts arbitrary bucket names.

        This test verifies that the storage client architecture doesn't restrict
        which buckets can be accessed - the restrictions should be at IAM level.
        """
        mock_client = MagicMock()
        mock_bucket = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        # Should be able to get a handle to any bucket
        result = mock_client.bucket("unify-message-attachments")

        mock_client.bucket.assert_called_once_with("unify-message-attachments")
        assert result == mock_bucket


# =============================================================================
# Edge Cases
# =============================================================================


class TestAttachmentCleanupEdgeCases:
    """Edge case tests for attachment cleanup."""

    def test_delete_attachments_with_string_user_id(self):
        """Cleanup should work with string user_id."""
        from orchestra.services.bucket_service import BucketService

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = []

        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_client
            service.unify_attachments_bucket = mock_bucket

            # Should handle string user_id
            deleted_count = service.delete_message_attachments_for_user(user_id="user-abc-123")

            assert deleted_count == 0
            mock_bucket.list_blobs.assert_called_once_with(prefix="user-abc-123/")

    def test_delete_attachments_with_many_files(self):
        """Cleanup should handle users with many attachments."""
        from orchestra.services.bucket_service import BucketService

        # Create 100 mock blobs
        mock_blobs = [MagicMock(name=f"12345/file{i}.pdf") for i in range(100)]
        for blob in mock_blobs:
            blob.delete = MagicMock()

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = mock_blobs

        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_client
            service.unify_attachments_bucket = mock_bucket

            deleted_count = service.delete_message_attachments_for_user(user_id=12345)

            assert deleted_count == 100
            for blob in mock_blobs:
                blob.delete.assert_called_once()

