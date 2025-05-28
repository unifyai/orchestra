import logging
import aiosmtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# --- SMTP Configuration ---
SMTP_HOSTNAME = settings.smtp_hostname
SMTP_PORT = settings.smtp_port
SMTP_USERNAME = settings.smtp_username
SMTP_PASSWORD = settings.smtp_password
SMTP_SENDER_EMAIL = settings.smtp_sender_email
SMTP_USE_STARTTLS = settings.smtp_use_starttls
SMTP_USE_SSL = settings.smtp_use_ssl

async def send_email_async(to_email: str, email_subject: str, email_body: str):
    """
    Sends an email to a user.
    """
    if not all([SMTP_HOSTNAME, SMTP_PORT, SMTP_SENDER_EMAIL]):
        logger.error(
            "SMTP settings (HOSTNAME, PORT, SENDER_EMAIL) are not fully configured. "
            "Cannot send actual email. Check your environment variables."
        )
        return

    msg = MIMEText(email_body, "plain", "utf-8")
    msg["Subject"] = Header(email_subject, "utf-8")
    msg["From"] = SMTP_SENDER_EMAIL
    msg["To"] = to_email

    try:
        smtp_client = aiosmtplib.SMTP(
            hostname=SMTP_HOSTNAME,
            port=SMTP_PORT,
            use_tls=SMTP_USE_SSL,  # If True, connects with SSL from the start
        )
        
        async with smtp_client:
            # If not using direct SSL from the start, and STARTTLS is enabled, upgrade connection
            if not SMTP_USE_SSL and SMTP_USE_STARTTLS:
                await smtp_client.starttls()
            
            # Login if credentials are provided
            if SMTP_USERNAME and SMTP_PASSWORD:
                await smtp_client.login(SMTP_USERNAME, SMTP_PASSWORD)
            
            await smtp_client.send_message(msg)
            logger.info(f"Email successfully sent to {to_email} via SMTP.")

    except aiosmtplib.SMTPException as e:
        logger.error(f"SMTP error sending email to {to_email}: {e.__class__.__name__} - {e}")
    except ConnectionRefusedError:
        logger.error(f"Connection refused when trying to send email to {to_email} via {SMTP_HOSTNAME}:{SMTP_PORT}.")
    except Exception as e:
        logger.error(f"An unexpected error occurred sending email to {to_email} via SMTP: {e.__class__.__name__} - {e}", exc_info=True)