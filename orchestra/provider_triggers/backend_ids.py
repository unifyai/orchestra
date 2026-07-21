"""Stable provider trigger backend identifiers."""

from __future__ import annotations

COMPOSIO_BACKEND_ID = "composio"
PIPEDREAM_BACKEND_ID = "pipedream"
NATIVE_GOOGLE_BACKEND_ID = "native_google"
NATIVE_MICROSOFT_BACKEND_ID = "native_microsoft"
LOCAL_BACKEND_ID = "local"

ASSISTANT_WORKSPACE_SECRETS_STORAGE = "assistant_workspace_secrets"

NATIVE_GOOGLE_MEET_APP_SLUG = "google_meet"
NATIVE_GOOGLE_DRIVE_APP_SLUG = "google_drive"
NATIVE_GOOGLE_CHAT_APP_SLUG = "google_chat"
NATIVE_MICROSOFT_TEAMS_APP_SLUG = "microsoft_teams"
NATIVE_MICROSOFT_OUTLOOK_APP_SLUG = "microsoft_outlook"
NATIVE_MICROSOFT_ONEDRIVE_APP_SLUG = "microsoft_onedrive"
NATIVE_MICROSOFT_GROUPS_APP_SLUG = "microsoft_groups"
NATIVE_MICROSOFT_DIRECTORY_APP_SLUG = "microsoft_directory"
NATIVE_MICROSOFT_TODO_APP_SLUG = "microsoft_todo"

NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG = "google.workspace.meet.transcript.v2.fileGenerated"
NATIVE_MICROSOFT_TEAMS_TRANSCRIPT_SLUG = (
    "microsoft.graph.onlineMeeting.transcript.created"
)

NATIVE_SIGNATURE_HEADERS = (
    "x-unify-webhook-id",
    "x-unify-webhook-timestamp",
    "x-unify-webhook-signature",
)
NATIVE_SIGNATURE_SCHEME = "native_hmac_sha256_timestamp_dot_body"

COMPOSIO_SIGNATURE_HEADERS = (
    "webhook-id",
    "webhook-timestamp",
    "webhook-signature",
)
COMPOSIO_SIGNATURE_SCHEME = "composio_v3_hmac_sha256"
PIPEDREAM_SIGNATURE_HEADERS = ("x-pd-signature",)
PIPEDREAM_SIGNATURE_SCHEME = "pipedream_hmac_sha256_timestamp_dot_body"
DEFAULT_SIGNATURE_TOLERANCE_SECONDS = 300
