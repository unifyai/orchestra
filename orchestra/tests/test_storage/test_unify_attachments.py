"""
Tests for assistant message attachment storage and cleanup functionality.

These tests verify:
- BucketService methods for attachment cleanup (assistant-centric and legacy user-based)
- User deletion includes attachment cleanup
- Signed URL generation works for the attachments bucket
- New bucket naming and backward-compatible aliases
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# BucketService - New Bucket Names and Aliases
# =============================================================================


class TestBucketServiceBucketNaming:
    """Tests for renamed bucket attributes and backward-compatible aliases."""

    def test_new_bucket_names_from_new_env_vars(self):
        """BucketService uses new env var names when available."""
        from orchestra.services.bucket_service import BucketService

        with patch.dict(
            os.environ,
            {
                "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
                "GCP_PROJECT_ID": "test-project",
                "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
                "ORCHESTRA_GCP_ASSISTANT_MEDIA_BUCKET_NAME": "assistant-media-production",
                "ORCHESTRA_GCP_ASSISTANT_MESSAGE_ATTACHMENTS_BUCKET_NAME": "assistant-message-attachments-production",
                "ORCHESTRA_GCP_ASSISTANT_CALL_RECORDINGS_BUCKET_NAME": "assistant-call-recordings-production",
            },
        ):
            with patch("orchestra.services.bucket_service.service_account"):
                with patch(
                    "orchestra.services.bucket_service.storage.Client",
                ) as mock_client:
                    mock_client.return_value = MagicMock()
                    service = BucketService()

                    assert (
                        service.assistant_media_bucket_name
                        == "assistant-media-production"
                    )
                    assert (
                        service.message_attachments_bucket_name
                        == "assistant-message-attachments-production"
                    )
                    assert (
                        service.call_recordings_bucket_name
                        == "assistant-call-recordings-production"
                    )

    def test_fallback_to_legacy_env_vars(self):
        """BucketService falls back to legacy env var names."""
        from orchestra.services.bucket_service import BucketService

        with patch.dict(
            os.environ,
            {
                "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
                "GCP_PROJECT_ID": "test-project",
                "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
                "ORCHESTRA_GCP_ASSISTANT_IMAGES_BUCKET_NAME": "hired_assistants_images",
                "ORCHESTRA_GCP_UNIFY_ATTACHMENTS_BUCKET_NAME": "unify-message-attachments",
                "ORCHESTRA_GCP_RECORDINGS_BUCKET_NAME": "unity-call-recordings",
            },
            clear=False,
        ):
            # Remove new env vars if set
            for key in [
                "ORCHESTRA_GCP_ASSISTANT_MEDIA_BUCKET_NAME",
                "ORCHESTRA_GCP_ASSISTANT_MESSAGE_ATTACHMENTS_BUCKET_NAME",
                "ORCHESTRA_GCP_ASSISTANT_CALL_RECORDINGS_BUCKET_NAME",
            ]:
                os.environ.pop(key, None)

            with patch("orchestra.services.bucket_service.service_account"):
                with patch(
                    "orchestra.services.bucket_service.storage.Client",
                ) as mock_client:
                    mock_client.return_value = MagicMock()
                    service = BucketService()

                    assert (
                        service.assistant_media_bucket_name == "hired_assistants_images"
                    )
                    assert (
                        service.message_attachments_bucket_name
                        == "unify-message-attachments"
                    )
                    assert (
                        service.call_recordings_bucket_name == "unity-call-recordings"
                    )

    def test_backward_compatible_aliases_exist(self):
        """Deprecated aliases still point to the correct buckets."""
        from orchestra.services.bucket_service import BucketService

        with patch.dict(
            os.environ,
            {
                "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
                "GCP_PROJECT_ID": "test-project",
                "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
            },
        ):
            with patch("orchestra.services.bucket_service.service_account"):
                with patch(
                    "orchestra.services.bucket_service.storage.Client",
                ) as mock_client:
                    mock_client.return_value = MagicMock()
                    service = BucketService()

                    # Aliases match new names
                    assert (
                        service.assistant_images_bucket_name
                        == service.assistant_media_bucket_name
                    )
                    assert (
                        service.assistant_images_bucket
                        is service.assistant_media_bucket
                    )
                    assert (
                        service.unify_attachments_bucket_name
                        == service.message_attachments_bucket_name
                    )
                    assert (
                        service.unify_attachments_bucket
                        is service.message_attachments_bucket
                    )
                    assert (
                        service.recordings_bucket_name
                        == service.call_recordings_bucket_name
                    )
                    assert service.recordings_bucket is service.call_recordings_bucket

    def test_presets_bucket_configured(self):
        """Presets bucket is configured with its own env var."""
        from orchestra.services.bucket_service import BucketService

        with patch.dict(
            os.environ,
            {
                "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
                "GCP_PROJECT_ID": "test-project",
                "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
            },
        ):
            with patch("orchestra.services.bucket_service.service_account"):
                with patch(
                    "orchestra.services.bucket_service.storage.Client",
                ) as mock_client:
                    mock_client.return_value = MagicMock()
                    service = BucketService()

                    assert service.presets_bucket_name == "assistant-media-presets"
                    assert service.presets_bucket is not None


# =============================================================================
# BucketService - Path Helper
# =============================================================================


class TestBuildAssistantPath:
    """Tests for the static build_assistant_path helper."""

    def test_build_path_with_string_id(self):
        from orchestra.services.bucket_service import BucketService

        path = BucketService.build_assistant_path("abc-123", "image", "photo.jpg")
        assert path == "abc-123/image/photo.jpg"

    def test_build_path_with_int_id(self):
        from orchestra.services.bucket_service import BucketService

        path = BucketService.build_assistant_path(42, "video", "clip.mp4")
        assert path == "42/video/clip.mp4"

    def test_build_path_voice(self):
        from orchestra.services.bucket_service import BucketService

        path = BucketService.build_assistant_path(99, "voice", "greeting.mp3")
        assert path == "99/voice/greeting.mp3"


# =============================================================================
# BucketService - Assistant Media Upload
# =============================================================================


class TestUploadAssistantMediaFile:
    """Tests for the new upload_assistant_media_file method."""

    @pytest.fixture
    def bucket_service(self):
        from orchestra.services.bucket_service import BucketService

        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_blob.upload_from_string.return_value = None

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.assistant_media_bucket = mock_bucket
            service.assistant_media_bucket_name = "assistant-media-production"
            yield service, mock_bucket, mock_blob

    def test_upload_with_assistant_id_image(self, bucket_service):
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_assistant_media_file(
            b"image-data",
            "image/jpeg",
            assistant_id=42,
        )
        assert gcs_url.startswith("gs://assistant-media-production/42/image/")
        assert gcs_url.endswith(".jpeg")

    def test_upload_with_assistant_id_video(self, bucket_service):
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_assistant_media_file(
            b"video-data",
            "video/mp4",
            assistant_id="abc",
        )
        assert gcs_url.startswith("gs://assistant-media-production/abc/video/")
        assert gcs_url.endswith(".mp4")

    def test_upload_with_assistant_id_audio(self, bucket_service):
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_assistant_media_file(
            b"audio-data",
            "audio/mpeg",
            assistant_id=7,
        )
        assert gcs_url.startswith("gs://assistant-media-production/7/voice/")

    def test_upload_with_user_id_legacy(self, bucket_service):
        """Legacy user_id path still works."""
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_assistant_media_file(
            b"image-data",
            "image/png",
            user_id="user-123",
        )
        assert gcs_url.startswith("gs://assistant-media-production/user-123/")
        assert "/image/" not in gcs_url  # Legacy path has no media_type subfolder

    def test_upload_assistant_id_takes_precedence(self, bucket_service):
        """assistant_id is used over user_id when both are provided."""
        service, mock_bucket, _ = bucket_service
        gcs_url = service.upload_assistant_media_file(
            b"data",
            "image/jpeg",
            assistant_id=42,
            user_id="user-123",
        )
        assert "42/image/" in gcs_url
        assert "user-123" not in gcs_url

    def test_upload_raises_without_id(self, bucket_service):
        """Raises ValueError when neither assistant_id nor user_id is given."""
        service, _, _ = bucket_service
        with pytest.raises(ValueError, match="assistant_id or user_id"):
            service.upload_assistant_media_file(b"data", "image/jpeg")


# =============================================================================
# BucketService - Temp File Upload
# =============================================================================


class TestUploadTempFile:
    """Tests for the new upload_temp_file and backward-compat wrapper."""

    @pytest.fixture
    def bucket_service(self):
        from orchestra.services.bucket_service import BucketService

        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_blob.upload_from_string.return_value = None
        mock_blob.generate_signed_url.return_value = "https://signed-url.example.com"

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.assistant_media_bucket = mock_bucket
            service.assistant_media_bucket_name = "assistant-media-production"
            yield service, mock_bucket, mock_blob

    def test_temp_file_uses_tmp_prefix(self, bucket_service):
        """Temp files are stored under tmp/ at bucket root."""
        service, mock_bucket, _ = bucket_service
        signed_url, gcs_uri = service.upload_temp_file(b"data", "image/jpeg")

        # Check the blob path starts with tmp/
        blob_path = mock_bucket.blob.call_args[0][0]
        assert blob_path.startswith("tmp/")
        assert gcs_uri.startswith("gs://assistant-media-production/tmp/")
        assert signed_url == "https://signed-url.example.com"

    def test_backward_compat_wrapper_ignores_user_id(self, bucket_service):
        """upload_temp_assistant_file wraps upload_temp_file, ignoring user_id."""
        service, mock_bucket, _ = bucket_service
        signed_url, gcs_uri = service.upload_temp_assistant_file(
            b"data",
            "user-999",
            "audio/wav",
        )

        blob_path = mock_bucket.blob.call_args[0][0]
        assert blob_path.startswith("tmp/")
        assert "user-999" not in blob_path
        assert "user-999" not in gcs_uri


# =============================================================================
# BucketService - Assistant Attachment Cleanup
# =============================================================================


class TestDeleteAssistantAttachments:
    """Tests for the new delete_assistant_attachments method."""

    @pytest.fixture
    def mock_storage_client(self):
        mock_blob1 = MagicMock()
        mock_blob1.name = "42/uuid1_file1.pdf"
        mock_blob1.delete = MagicMock()

        mock_blob2 = MagicMock()
        mock_blob2.name = "42/uuid2_file2.png"
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

    def test_delete_assistant_attachments_returns_count(self, mock_storage_client):
        from orchestra.services.bucket_service import BucketService

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_storage_client["client"]
            service.message_attachments_bucket = mock_storage_client["bucket"]

            deleted_count = service.delete_assistant_attachments(assistant_id=42)

            assert deleted_count == 2
            mock_storage_client["bucket"].list_blobs.assert_called_once_with(
                prefix="42/",
            )
            for blob in mock_storage_client["blobs"]:
                blob.delete.assert_called_once()

    def test_delete_assistant_attachments_empty(self, mock_storage_client):
        from orchestra.services.bucket_service import BucketService

        mock_storage_client["bucket"].list_blobs.return_value = []

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_storage_client["client"]
            service.message_attachments_bucket = mock_storage_client["bucket"]

            deleted_count = service.delete_assistant_attachments(assistant_id=999)
            assert deleted_count == 0


# =============================================================================
# BucketService - Legacy Attachment Cleanup (backward compat)
# =============================================================================


class TestBucketServiceAttachmentCleanup:
    """Tests for BucketService.delete_message_attachments_for_user method (legacy)."""

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
            service.message_attachments_bucket = mock_storage_client["bucket"]

            deleted_count = service.delete_message_attachments_for_user(user_id=12345)

            assert deleted_count == 2
            mock_storage_client["bucket"].list_blobs.assert_called_once_with(
                prefix="12345/",
            )
            for blob in mock_storage_client["blobs"]:
                blob.delete.assert_called_once()

    def test_delete_attachments_for_user_with_no_files(self, mock_storage_client):
        """delete_message_attachments_for_user returns 0 when user has no attachments."""
        from orchestra.services.bucket_service import BucketService

        mock_storage_client["bucket"].list_blobs.return_value = []

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_storage_client["client"]
            service.message_attachments_bucket = mock_storage_client["bucket"]

            deleted_count = service.delete_message_attachments_for_user(user_id=99999)

            assert deleted_count == 0
            mock_storage_client["bucket"].list_blobs.assert_called_once_with(
                prefix="99999/",
            )

    def test_delete_attachments_handles_deletion_errors_gracefully(
        self,
        mock_storage_client,
    ):
        """Deletion continues even if individual file deletion fails."""
        from google.api_core.exceptions import GoogleAPIError

        from orchestra.services.bucket_service import BucketService

        # First blob fails, second succeeds
        mock_storage_client["blobs"][0].delete.side_effect = GoogleAPIError("Failed")
        mock_storage_client["blobs"][1].delete.return_value = None

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_storage_client["client"]
            service.message_attachments_bucket = mock_storage_client["bucket"]

            # Should return count of successful deletions only
            deleted_count = service.delete_message_attachments_for_user(user_id=12345)

            assert deleted_count == 1  # Only one succeeded

    def test_bucket_service_has_attachments_bucket_configured(self):
        """BucketService should have the message attachments bucket configured."""
        from orchestra.services.bucket_service import BucketService

        with patch.dict(
            os.environ,
            {
                "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path",
                "GCP_PROJECT_ID": "test-project",
                "ORCHESTRA_GCP_BUCKET_NAME": "test-bucket",
            },
        ):
            with patch("orchestra.services.bucket_service.service_account"):
                with patch(
                    "orchestra.services.bucket_service.storage.Client",
                ) as mock_client:
                    mock_client.return_value = MagicMock()
                    service = BucketService()

                    # New name
                    assert (
                        service.message_attachments_bucket_name
                        == "assistant-message-attachments"
                    )
                    assert service.message_attachments_bucket is not None

                    # Backward-compat alias
                    assert (
                        service.unify_attachments_bucket_name
                        == service.message_attachments_bucket_name
                    )
                    assert service.unify_attachments_bucket is not None


# =============================================================================
# BucketService - delete_all_assistant_data
# =============================================================================


class TestDeleteAllAssistantData:
    """Tests for the convenience delete_all_assistant_data method."""

    @pytest.fixture
    def bucket_service_with_mocks(self):
        from orchestra.services.bucket_service import BucketService

        media_blob = MagicMock()
        media_blob.name = "42/image/photo.jpg"
        media_blob.delete = MagicMock()

        media_bucket = MagicMock()
        media_bucket.list_blobs.return_value = [media_blob]

        recordings_bucket = MagicMock()
        recordings_bucket.list_blobs.return_value = []

        attachments_bucket = MagicMock()
        attachments_bucket.list_blobs.return_value = []

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.assistant_media_bucket = media_bucket
            service.assistant_media_bucket_name = "assistant-media-production"
            service.call_recordings_bucket = recordings_bucket
            service.call_recordings_bucket_name = "assistant-call-recordings-production"
            service.message_attachments_bucket = attachments_bucket
            service.message_attachments_bucket_name = (
                "assistant-message-attachments-production"
            )
            yield service

    def test_delete_all_returns_summary(self, bucket_service_with_mocks):
        result = bucket_service_with_mocks.delete_all_assistant_data(42)

        assert isinstance(result, dict)
        assert "media" in result
        assert "recordings" in result
        assert "attachments" in result
        assert result["media"] == 1
        assert result["recordings"] == 0
        assert result["attachments"] == 0


# =============================================================================
# BucketService - Recording Deletion with Legacy Paths
# =============================================================================


class TestDeleteAssistantRecordings:
    """Tests for recording deletion with both new and legacy paths."""

    @pytest.fixture
    def bucket_service_with_recording_mocks(self):
        from orchestra.services.bucket_service import BucketService

        new_blob = MagicMock()
        new_blob.name = "42/call.mp3"
        new_blob.delete = MagicMock()

        legacy_blob = MagicMock()
        legacy_blob.name = "production/42/old_call.mp3"
        legacy_blob.delete = MagicMock()

        recordings_bucket = MagicMock()

        def list_blobs_side_effect(prefix=None):
            if prefix == "42/":
                return [new_blob]
            elif prefix == "production/42/":
                return [legacy_blob]
            return []

        recordings_bucket.list_blobs.side_effect = list_blobs_side_effect

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.call_recordings_bucket = recordings_bucket
            service.call_recordings_bucket_name = "assistant-call-recordings-production"
            yield service, new_blob, legacy_blob

    def test_deletes_from_new_and_legacy_paths(
        self,
        bucket_service_with_recording_mocks,
    ):
        service, new_blob, legacy_blob = bucket_service_with_recording_mocks

        count = service.delete_assistant_recordings(42)

        assert count == 2
        new_blob.delete.assert_called_once()
        legacy_blob.delete.assert_called_once()


# =============================================================================
# User Account Deletion - Attachment Cleanup Integration
# =============================================================================


class TestUserDeletionAttachmentCleanup:
    """Tests for GCS cleanup during user account deletion."""

    @staticmethod
    def _make_mock_session(assistant_ids: list[int] | None = None):
        """Build a mock session that passes blocker checks and returns assistant IDs."""
        mock_session = MagicMock()

        # -- check_deletion_blockers result (fetchone #1) --
        mock_result = MagicMock()
        mock_result.user_exists = True
        mock_result.has_pending_bills = False
        mock_result.pending_amount = 0
        mock_result.has_disputed_charges = False
        mock_result.account_status = None
        mock_result.owns_organizations = False
        mock_result.owned_org_names = None

        # -- billing account lookup result (fetchone #2) --
        mock_ba_info = MagicMock()
        mock_ba_info.ba_id = None
        mock_ba_info.stripe_customer_id = None

        mock_session.execute.return_value.fetchone.side_effect = [
            mock_result,  # check_deletion_blockers
            mock_ba_info,  # billing account lookup
        ]

        # -- _get_user_assistant_ids result (fetchall) --
        if assistant_ids is not None:
            rows = [(aid,) for aid in assistant_ids]
        else:
            rows = []
        mock_session.execute.return_value.fetchall.return_value = rows

        return mock_session

    def test_user_deletion_cleans_up_all_assistant_data(self):
        """delete_user_account should call delete_all_assistant_data for each assistant."""
        from orchestra.services.user_account_cleanup_service import (
            UserAccountCleanupService,
        )

        mock_session = self._make_mock_session(assistant_ids=[10, 20])

        with patch(
            "orchestra.services.bucket_service.BucketService",
        ) as mock_bucket_cls:
            mock_bucket_service = MagicMock()
            mock_bucket_service.delete_all_assistant_data.return_value = {
                "media": 1,
                "recordings": 0,
                "attachments": 2,
            }
            mock_bucket_cls.return_value = mock_bucket_service

            service = UserAccountCleanupService(mock_session)
            result = service.delete_user_account("user-123")

            assert result.success is True
            assert mock_bucket_service.delete_all_assistant_data.call_count == 2
            mock_bucket_service.delete_all_assistant_data.assert_any_call(10)
            mock_bucket_service.delete_all_assistant_data.assert_any_call(20)

    def test_user_deletion_falls_back_to_legacy_cleanup_when_no_assistants(self):
        """When no assistants found, fall back to legacy user-prefix cleanup."""
        from orchestra.services.user_account_cleanup_service import (
            UserAccountCleanupService,
        )

        mock_session = self._make_mock_session(assistant_ids=[])

        with patch(
            "orchestra.services.bucket_service.BucketService",
        ) as mock_bucket_cls:
            mock_bucket_service = MagicMock()
            mock_bucket_service.delete_message_attachments_for_user.return_value = 3
            mock_bucket_cls.return_value = mock_bucket_service

            service = UserAccountCleanupService(mock_session)
            result = service.delete_user_account("user-123")

            assert result.success is True
            mock_bucket_service.delete_message_attachments_for_user.assert_called_once_with(
                "user-123",
            )
            mock_bucket_service.delete_all_assistant_data.assert_not_called()

    def test_user_deletion_continues_if_gcs_cleanup_fails(self):
        """User deletion should succeed even if GCS cleanup raises."""
        from orchestra.services.user_account_cleanup_service import (
            UserAccountCleanupService,
        )

        mock_session = self._make_mock_session(assistant_ids=[10])

        with patch(
            "orchestra.services.bucket_service.BucketService",
        ) as mock_bucket_cls:
            mock_bucket_cls.side_effect = Exception("GCS error")

            service = UserAccountCleanupService(mock_session)
            result = service.delete_user_account("user-123")

            assert result.success is True

    def test_user_deletion_continues_if_single_assistant_cleanup_fails(self):
        """If one assistant's GCS cleanup fails, the others still proceed."""
        from orchestra.services.user_account_cleanup_service import (
            UserAccountCleanupService,
        )

        mock_session = self._make_mock_session(assistant_ids=[10, 20, 30])

        with patch(
            "orchestra.services.bucket_service.BucketService",
        ) as mock_bucket_cls:
            mock_bucket_service = MagicMock()

            def side_effect(aid, **kwargs):
                if aid == 20:
                    raise Exception("Bucket unavailable")
                return {"media": 1, "recordings": 0, "attachments": 0}

            mock_bucket_service.delete_all_assistant_data.side_effect = side_effect
            mock_bucket_cls.return_value = mock_bucket_service

            service = UserAccountCleanupService(mock_session)
            result = service.delete_user_account("user-123")

            assert result.success is True
            # All three should have been attempted
            assert mock_bucket_service.delete_all_assistant_data.call_count == 3


# =============================================================================
# Storage Endpoints - Attachments Bucket Access
# =============================================================================


class TestStorageEndpointsAttachmentsBucket:
    """Tests for storage endpoints with the message-attachments bucket.

    Note: These tests verify the GCS URL parsing logic works correctly with
    the attachments bucket name. The actual endpoint tests with database
    are in test_storage.py.
    """

    def test_gcs_url_parsing_for_new_bucket_name(self):
        """parse_gcs_url correctly parses assistant-message-attachments bucket URIs."""
        from orchestra.web.api.utils.gcp import parse_gcs_url

        bucket, path = parse_gcs_url(
            "gs://assistant-message-attachments/42/uuid_document.pdf",
        )
        assert bucket == "assistant-message-attachments"
        assert path == "42/uuid_document.pdf"

    def test_gcs_url_parsing_for_legacy_bucket_name(self):
        """parse_gcs_url still works with old unify-message-attachments name."""
        from orchestra.web.api.utils.gcp import parse_gcs_url

        bucket, path = parse_gcs_url(
            "gs://unify-message-attachments/12345/uuid_document.pdf",
        )
        assert bucket == "unify-message-attachments"
        assert path == "12345/uuid_document.pdf"

    def test_gcs_url_parsing_assistant_centric_path(self):
        """parse_gcs_url handles new assistant-centric nested paths."""
        from orchestra.web.api.utils.gcp import parse_gcs_url

        bucket, path = parse_gcs_url(
            "gs://assistant-media-production/42/image/photo_abc123.jpg",
        )
        assert bucket == "assistant-media-production"
        assert path == "42/image/photo_abc123.jpg"

    def test_storage_client_can_access_any_bucket(self):
        """Verify storage client bucket() method accepts arbitrary bucket names."""
        mock_client = MagicMock()
        mock_bucket = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        result = mock_client.bucket("assistant-message-attachments")

        mock_client.bucket.assert_called_once_with("assistant-message-attachments")
        assert result == mock_bucket


# =============================================================================
# Edge Cases
# =============================================================================


class TestAttachmentCleanupEdgeCases:
    """Edge case tests for attachment cleanup."""

    def test_delete_attachments_with_string_user_id(self):
        """Legacy cleanup should work with string user_id."""
        from orchestra.services.bucket_service import BucketService

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = []

        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_client
            service.message_attachments_bucket = mock_bucket

            deleted_count = service.delete_message_attachments_for_user(
                user_id="user-abc-123",
            )

            assert deleted_count == 0
            mock_bucket.list_blobs.assert_called_once_with(prefix="user-abc-123/")

    def test_delete_assistant_attachments_with_int_id(self):
        """Assistant-centric cleanup works with integer assistant_id."""
        from orchestra.services.bucket_service import BucketService

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = []

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.message_attachments_bucket = mock_bucket

            deleted_count = service.delete_assistant_attachments(assistant_id=42)

            assert deleted_count == 0
            mock_bucket.list_blobs.assert_called_once_with(prefix="42/")

    def test_delete_attachments_with_many_files(self):
        """Cleanup should handle assistants with many attachments."""
        from orchestra.services.bucket_service import BucketService

        mock_blobs = [MagicMock(name=f"42/file{i}.pdf") for i in range(100)]
        for blob in mock_blobs:
            blob.delete = MagicMock()

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = mock_blobs

        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch.object(BucketService, "__init__", lambda x: None):
            service = BucketService()
            service.storage_client = mock_client
            service.message_attachments_bucket = mock_bucket

            deleted_count = service.delete_assistant_attachments(assistant_id=42)

            assert deleted_count == 100
            for blob in mock_blobs:
                blob.delete.assert_called_once()
