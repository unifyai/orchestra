"""Filesystem-backed media storage for self-host deployments."""

from __future__ import annotations

import base64
import datetime
import hashlib
import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple

from orchestra.settings import settings
from orchestra.web.api.utils.gcp import parse_gcs_url

logger = logging.getLogger(__name__)

_MIME_EXT_OVERRIDES = {
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/x-aac": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp3": "mp3",
}


def _extension_from_content_type(content_type: str, fallback: str = "bin") -> str:
    if not content_type:
        return fallback
    content_type = content_type.lower().strip()
    if content_type in _MIME_EXT_OVERRIDES:
        return _MIME_EXT_OVERRIDES[content_type]
    extension = mimetypes.guess_extension(content_type, strict=False)
    if extension:
        return extension.lstrip(".")
    return content_type.split("/")[-1] if "/" in content_type else fallback


def _media_type_from_content_type(content_type: str) -> str:
    if not content_type:
        return "image"
    content_type = content_type.lower()
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("audio/"):
        return "voice"
    return "image"


def _orchestra_public_base_url() -> str:
    configured = os.environ.get("ORCHESTRA_PUBLIC_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    host = os.environ.get("ORCHESTRA_HOST", "127.0.0.1")
    port = os.environ.get("ORCHESTRA_PORT", "8000")
    return f"http://{host}:{port}"


@dataclass
class LocalBlob:
    """Minimal GCS blob stand-in backed by a local file."""

    name: str
    path: Path
    content_type: str | None = None

    @property
    def updated(self) -> datetime.datetime:
        if not self.path.is_file():
            return datetime.datetime.now(datetime.timezone.utc)
        timestamp = self.path.stat().st_mtime
        return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)

    @property
    def size(self) -> int:
        if not self.path.is_file():
            return 0
        return self.path.stat().st_size

    def exists(self) -> bool:
        return self.path.is_file()

    def reload(self) -> None:
        return None

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)
        self.content_type = content_type

    def download_as_bytes(self) -> bytes:
        return self.path.read_bytes()

    def delete(self) -> None:
        if self.path.is_file():
            self.path.unlink()

    def generate_signed_url(
        self,
        *,
        version: str,
        expiration: datetime.timedelta,
        method: str = "GET",
        response_disposition: str | None = None,
    ) -> str:
        bucket_name, object_path = parse_gcs_url(f"gs://{self.name}")
        if not bucket_name or not object_path:
            bucket_name = self.path.parent.name
            object_path = self.path.name
        return local_object_http_url(bucket_name, object_path)


class LocalBucket:
    """Minimal GCS bucket stand-in for one named local bucket."""

    def __init__(self, service: LocalBucketService, bucket_name: str) -> None:
        self._service = service
        self._bucket_name = bucket_name

    def blob(self, object_path: str) -> LocalBlob:
        path = self._service._object_path(self._bucket_name, object_path)
        return LocalBlob(
            name=f"{self._bucket_name}/{object_path}",
            path=path,
        )

    def list_blobs(self, prefix: str = "") -> Iterator[LocalBlob]:
        root = self._service._bucket_root(self._bucket_name) / prefix
        if not root.exists():
            return iter(())
        blobs: list[LocalBlob] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self._service._bucket_root(self._bucket_name))
            blobs.append(
                LocalBlob(
                    name=f"{self._bucket_name}/{relative.as_posix()}",
                    path=path,
                ),
            )
        return iter(blobs)


class LocalStorageClient:
    """Minimal GCS client stand-in for local bucket access."""

    def __init__(self, service: LocalBucketService) -> None:
        self._service = service

    def bucket(self, bucket_name: str) -> LocalBucket:
        return LocalBucket(self._service, bucket_name)


def local_object_http_url(bucket_name: str, object_path: str) -> str:
    """Return an Orchestra HTTP URL for a locally stored object."""
    return (
        f"{_orchestra_public_base_url()}/v0/storage/local/"
        f"{bucket_name}/{object_path.lstrip('/')}"
    )


class LocalBucketService:
    """Persist Orchestra media on disk instead of GCS when ``SELF_HOST=1``."""

    @staticmethod
    def build_assistant_path(
        assistant_id: str | int,
        media_type: str,
        filename: str,
    ) -> str:
        return f"{assistant_id}/{media_type}/{filename}"

    def __init__(self) -> None:
        root = os.environ.get(
            "SELF_HOST_LOCAL_BUCKET_DIR",
            str(Path.home() / ".unity" / "local-bucket"),
        )
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

        env_suffix = "staging" if settings.is_staging else "production"
        self.bucket_name = os.getenv("ORCHESTRA_GCP_BUCKET_NAME", "local-bucket")
        self.assistant_media_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_MEDIA_BUCKET_NAME",
            f"assistant-media-{env_suffix}",
        )
        self.message_attachments_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_MESSAGE_ATTACHMENTS_BUCKET_NAME",
            f"assistant-message-attachments-{env_suffix}",
        )
        self.call_recordings_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_CALL_RECORDINGS_BUCKET_NAME",
            f"assistant-call-recordings-{env_suffix}",
        )
        self.account_photo_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ACCOUNT_PHOTO_BUCKET_NAME",
            f"account-photo-{env_suffix}",
        )
        self.presets_bucket_name = os.getenv(
            "ORCHESTRA_GCP_ASSISTANT_MEDIA_PRESETS_BUCKET_NAME",
            "assistant-media-presets",
        )
        self._allowed_buckets = {
            self.bucket_name,
            self.assistant_media_bucket_name,
            self.message_attachments_bucket_name,
            self.call_recordings_bucket_name,
            self.account_photo_bucket_name,
            self.presets_bucket_name,
        }

        self.storage_client = LocalStorageClient(self)
        self.bucket = LocalBucket(self, self.bucket_name)
        self.assistant_media_bucket = LocalBucket(
            self,
            self.assistant_media_bucket_name,
        )
        self.message_attachments_bucket = LocalBucket(
            self,
            self.message_attachments_bucket_name,
        )
        self.call_recordings_bucket = LocalBucket(
            self,
            self.call_recordings_bucket_name,
        )
        self.account_photo_bucket = LocalBucket(self, self.account_photo_bucket_name)
        self.presets_bucket = LocalBucket(self, self.presets_bucket_name)

    def _bucket_root(self, bucket_name: str) -> Path:
        return self._root / bucket_name

    def _object_path(self, bucket_name: str, object_path: str) -> Path:
        bucket_root = self._bucket_root(bucket_name).resolve()
        path = (bucket_root / object_path.lstrip("/")).resolve()
        if bucket_root != path and bucket_root not in path.parents:
            raise ValueError("Object path escapes bucket root")
        return path

    def _write_object(
        self,
        bucket_name: str,
        object_path: str,
        content: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        path = self._object_path(bucket_name, object_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if content_type:
            sidecar = path.with_suffix(path.suffix + ".content-type")
            sidecar.write_text(content_type, encoding="utf-8")
        return f"gs://{bucket_name}/{object_path}"

    @staticmethod
    def _generate_unique_filename(content: bytes, extension: str = "") -> str:
        content_hash = hashlib.md5(content).hexdigest()
        unique_id = str(uuid.uuid4())[:8]
        if extension:
            return f"{content_hash}_{unique_id}.{extension.lstrip('.')}"
        return f"{content_hash}_{unique_id}"

    def is_allowed_bucket(self, bucket_name: str) -> bool:
        return bucket_name in self._allowed_buckets

    def upload_media(self, base64_media: str, media_type: str) -> Tuple[str, str]:
        if "," in base64_media:
            base64_media = base64_media.split(",")[1]
        media_content = base64.b64decode(base64_media)
        extension = mimetypes.guess_extension(media_type) or ""
        filename = self._generate_unique_filename(media_content, extension)
        gcs_uri = self._write_object(
            self.bucket_name,
            filename,
            media_content,
            content_type=media_type,
        )
        return local_object_http_url(self.bucket_name, filename), filename

    def get_media(self, filename: str) -> Optional[str]:
        path = self._object_path(self.bucket_name, filename)
        if not path.is_file():
            return None
        return base64.b64encode(path.read_bytes()).decode("utf-8")

    def delete_media(self, filename: str) -> bool:
        path = self._object_path(self.bucket_name, filename)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def get_media_url(self, filename: str) -> str:
        return local_object_http_url(self.bucket_name, filename)

    def upload_assistant_media_file(
        self,
        file_content: bytes,
        content_type: str = "image/jpeg",
        *,
        assistant_id: str | int | None = None,
        user_id: str | None = None,
    ) -> str:
        if not assistant_id and not user_id:
            raise ValueError(
                "Either assistant_id or user_id must be provided for upload path.",
            )
        extension = (
            content_type.split("/")[-1]
            if content_type and "/" in content_type
            else "jpg"
        )
        file_name = self._generate_unique_filename(file_content)
        if assistant_id:
            media_type = _media_type_from_content_type(content_type)
            object_path = self.build_assistant_path(
                assistant_id,
                media_type,
                f"{file_name}.{extension}",
            )
        else:
            object_path = f"{user_id}/{file_name}.{extension}"
        return self._write_object(
            self.assistant_media_bucket_name,
            object_path,
            file_content,
            content_type=content_type,
        )

    def upload_assistant_photo_file(
        self,
        file_content: bytes,
        user_id: str,
        content_type: str = "image/jpeg",
        *,
        assistant_id: str | int | None = None,
    ) -> str:
        return self.upload_assistant_media_file(
            file_content,
            content_type,
            assistant_id=assistant_id,
            user_id=user_id,
        )

    def upload_user_photo_file(
        self,
        file_content: bytes,
        user_id: str,
        content_type: str = "image/jpeg",
    ) -> str:
        extension = (
            content_type.split("/")[-1]
            if content_type and "/" in content_type
            else "jpg"
        )
        file_name = self._generate_unique_filename(file_content)
        object_path = f"user/{user_id}/{file_name}.{extension}"
        return self._write_object(
            self.account_photo_bucket_name,
            object_path,
            file_content,
            content_type=content_type,
        )

    def upload_org_photo_file(
        self,
        file_content: bytes,
        org_id: int,
        content_type: str = "image/jpeg",
    ) -> str:
        extension = (
            content_type.split("/")[-1]
            if content_type and "/" in content_type
            else "jpg"
        )
        file_name = self._generate_unique_filename(file_content)
        object_path = f"organization/{org_id}/{file_name}.{extension}"
        return self._write_object(
            self.account_photo_bucket_name,
            object_path,
            file_content,
            content_type=content_type,
        )

    def upload_temp_file(
        self,
        file_content: bytes,
        content_type: str,
    ) -> Tuple[str, str]:
        extension = _extension_from_content_type(content_type)
        file_name = self._generate_unique_filename(file_content)
        object_path = f"tmp/{file_name}.{extension}"
        gcs_uri = self._write_object(
            self.assistant_media_bucket_name,
            object_path,
            file_content,
            content_type=content_type,
        )
        signed_url = local_object_http_url(
            self.assistant_media_bucket_name,
            object_path,
        )
        return signed_url, gcs_uri

    def upload_temp_assistant_file(
        self,
        file_content: bytes,
        user_id: str,
        content_type: str,
    ) -> Tuple[str, str]:
        return self.upload_temp_file(
            file_content=file_content,
            content_type=content_type,
        )

    def delete_assistant_file(self, gcs_url: str) -> bool:
        parsed_bucket, object_path = parse_gcs_url(gcs_url)
        if not parsed_bucket or not object_path:
            logger.warning("Invalid GCS URL for deletion: %s", gcs_url)
            return False
        if parsed_bucket != self.assistant_media_bucket_name:
            logger.error(
                "Attempt to delete file from incorrect bucket. "
                "Expected '%s', got '%s'. URL: %s",
                self.assistant_media_bucket_name,
                parsed_bucket,
                gcs_url,
            )
            return False
        path = self._object_path(parsed_bucket, object_path)
        if path.is_file():
            path.unlink()
        return True

    def delete_assistant_recordings(
        self,
        assistant_id: str | int,
        *,
        is_staging: bool = False,
    ) -> int:
        prefixes = [
            f"{assistant_id}/",
            f"staging/{assistant_id}/",
            f"production/{assistant_id}/",
        ]
        deleted_count = 0
        for prefix in prefixes:
            for blob in self.call_recordings_bucket.list_blobs(prefix=prefix):
                if blob.path.is_file():
                    blob.delete()
                    deleted_count += 1
        return deleted_count

    def delete_assistant_attachments(self, assistant_id: str | int) -> int:
        deleted_count = 0
        for blob in self.message_attachments_bucket.list_blobs(
            prefix=f"{assistant_id}/",
        ):
            if blob.path.is_file():
                blob.delete()
                deleted_count += 1
        return deleted_count

    def delete_message_attachments_for_user(self, user_id: str | int) -> int:
        deleted_count = 0
        for blob in self.message_attachments_bucket.list_blobs(prefix=f"{user_id}/"):
            if blob.path.is_file():
                blob.delete()
                deleted_count += 1
        return deleted_count

    def delete_all_assistant_data(
        self,
        assistant_id: str | int,
        *,
        is_staging: bool = False,
    ) -> dict:
        media_count = 0
        for blob in self.assistant_media_bucket.list_blobs(prefix=f"{assistant_id}/"):
            if blob.path.is_file():
                blob.delete()
                media_count += 1
        recordings_count = self.delete_assistant_recordings(
            assistant_id,
            is_staging=is_staging,
        )
        attachments_count = self.delete_assistant_attachments(assistant_id)
        return {
            "media": media_count,
            "recordings": recordings_count,
            "attachments": attachments_count,
        }

    def delete_user_account_photos(self, user_id: str) -> int:
        return self._delete_account_photo_prefix(f"user/{user_id}/")

    def delete_org_account_photos(self, org_id: int) -> int:
        return self._delete_account_photo_prefix(f"organization/{org_id}/")

    def _delete_account_photo_prefix(self, prefix: str) -> int:
        deleted = 0
        for blob in self.account_photo_bucket.list_blobs(prefix=prefix):
            if blob.path.is_file():
                blob.delete()
                deleted += 1
        return deleted

    def read_local_object(
        self,
        bucket_name: str,
        object_path: str,
    ) -> tuple[bytes, str | None]:
        """Load bytes and optional content type for a locally stored object."""
        if not self.is_allowed_bucket(bucket_name):
            raise FileNotFoundError(bucket_name)
        try:
            path = self._object_path(bucket_name, object_path)
        except ValueError as exc:
            raise FileNotFoundError(object_path) from exc
        if not path.is_file():
            raise FileNotFoundError(object_path)
        content_type = None
        sidecar = path.with_suffix(path.suffix + ".content-type")
        if sidecar.is_file():
            content_type = sidecar.read_text(encoding="utf-8")
        if not content_type:
            content_type, _ = mimetypes.guess_type(path.name)
        return path.read_bytes(), content_type
