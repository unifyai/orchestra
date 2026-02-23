import asyncio
import base64
import logging
from email.mime.text import MIMEText

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from orchestra.settings import settings

logger = logging.getLogger(__name__)

# --- Gmail API Configuration with Service Account ---
SERVICE_ACCOUNT_FILE = (
    settings.google_service_account_key_path
)  # Path to the service account JSON key file.
DELEGATED_USER_EMAIL = (
    settings.google_service_sender_email
)  # The email address of the Google Workspace user the service account will impersonate.

# Scopes required for sending email via Gmail API
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def _send_email_sync(
    to_email: str,
    email_subject: str,
    email_body: str,
    sender_email: str,
    impersonate_email: str | None = None,
) -> bool:
    """
    Synchronous implementation of email sending via Gmail API.
    This is called via run_in_executor to avoid blocking the event loop.

    Args:
        to_email: Recipient email address.
        email_subject: Email subject line.
        email_body: HTML email body.
        sender_email: The "From" address shown in the email.
        impersonate_email: The actual user to impersonate (must be a real user in Google Workspace).
                          If sender_email is an alias, this should be the primary email.
                          Defaults to DELEGATED_USER_EMAIL.
    """
    try:
        # Create credentials from the service account file, impersonating the user
        # Note: We impersonate the delegated user (must be real), but can send "from" an alias
        impersonate_as = impersonate_email or DELEGATED_USER_EMAIL
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=SCOPES,
            subject=impersonate_as,  # Impersonate this user (must be real user, not alias)
        )

        # Build the Gmail service
        service = build(
            "gmail",
            "v1",
            credentials=creds,
            cache_discovery=False,
        )

        # Create the email message
        message = MIMEText(email_body, "html")
        message["to"] = to_email
        message["from"] = sender_email
        message["subject"] = email_subject

        # Encode the message in base64url format
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message_body = {"raw": encoded_message}

        # Send the message using the Gmail API
        # 'me' refers to the authenticated user (which is DELEGATED_USER_EMAIL due to impersonation)
        send_message = (
            service.users()
            .messages()
            .send(userId="me", body=create_message_body)
            .execute()
        )
        logger.info(
            f"Email successfully sent from {sender_email} to {to_email} via Gmail API. "
            f"Message ID: {send_message.get('id')}",
        )
        return True

    except HttpError as error:
        logger.error(
            f"An HTTP error occurred sending email to {to_email} via Gmail API: {error}",
            exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"An unexpected error occurred sending email to {to_email} via Gmail API: {e}",
            exc_info=True,
        )
        return False


async def send_email_async(
    to_email: str,
    email_subject: str,
    email_body: str,
    from_email: str | None = None,
    impersonate_email: str | None = None,
) -> bool:
    """
    Sends an email using Gmail API with OAuth 2.0 (Service Account with Domain-Wide Delegation).

    The Gmail API client is synchronous, so this function runs the blocking I/O
    in a thread pool executor to avoid blocking the async event loop.

    Args:
        to_email: Recipient email address.
        email_subject: Email subject line.
        email_body: HTML email body.
        from_email: Sender "From" address shown in the email. Defaults to DELEGATED_USER_EMAIL.
                    Can be an alias of the impersonated user.
        impersonate_email: The actual user to impersonate (must be a real user in Google Workspace).
                           Defaults to DELEGATED_USER_EMAIL. Required when from_email is an alias.
    """
    if not SERVICE_ACCOUNT_FILE:
        logger.error(
            "Google Service Account Key Path not configured. Cannot send email via OAuth.",
        )
        return False

    sender_email = from_email or DELEGATED_USER_EMAIL
    if not sender_email:
        logger.error(
            "No sender email configured. Set ONBOARDING_EMAIL env var or pass from_email.",
        )
        return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,  # Use the default thread pool executor
        _send_email_sync,
        to_email,
        email_subject,
        email_body,
        sender_email,
        impersonate_email,
    )
