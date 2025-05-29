import logging
import base64
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


async def send_email_async(to_email: str, email_subject: str, email_body: str) -> bool:
    """
    Sends an email using Gmail API with OAuth 2.0 (Service Account with Domain-Wide Delegation).
    """
    if not SERVICE_ACCOUNT_FILE or not os.path.exists(SERVICE_ACCOUNT_FILE):
        logger.error(
            "Google Service Account Key Path not configured. Cannot send email via OAuth."
        )
        return False
    if not DELEGATED_USER_EMAIL:
        logger.error(
            "Delegated user email (sender email) not configured. Cannot send email via OAuth."
        )
        return False

    try:
        # Create credentials from the service account file, impersonating the user
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=SCOPES,
            subject=DELEGATED_USER_EMAIL,  # Impersonate this user
        )

        # Build the Gmail service
        service = build(
            "gmail", "v1", credentials=creds, cache_discovery=False
        )  # Added cache_discovery=False

        # Create the email message
        message = MIMEText(email_body, "html")
        message["to"] = to_email
        message["from"] = DELEGATED_USER_EMAIL
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
            f"Email successfully sent to {to_email} via Gmail API. Message ID: {send_message.get('id')}"
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
