"""Clean up expired email verification records.

Runs periodically via scheduled job to remove verification codes that
have expired (signup codes after 1 hour, password reset codes after
10 minutes). This keeps the email_verification table small.

Scheduling Options:
1. GitHub Actions: .github/workflows/cleanup-email-verifications.yml
   - Runs daily at 3:30 AM UTC via cron: '30 3 * * *'
   - Calls POST /v0/admin/cleanup/email-verifications

2. Cloud Scheduler: Create a job to call the admin endpoint
   - Schedule: Daily or every 6 hours
   - Endpoint: POST /v0/admin/cleanup/email-verifications
   - Headers: Authorization: Bearer <ORCHESTRA_ADMIN_KEY>

3. Manual: Call the admin endpoint directly for one-off cleanup
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.email_verification_dao import EmailVerificationDAO
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


def cleanup_expired_verifications(session: Optional[Session] = None) -> int:
    """
    Remove expired email verification records.

    Args:
        session: Optional database session. If not provided, creates one.

    Returns:
        Number of records deleted.
    """
    if session is not None:
        return _cleanup_in_session(session)
    else:
        SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
        with SessionLocal() as session:
            return _cleanup_in_session(session)


def _cleanup_in_session(session: Session) -> int:
    """Internal function to clean up expired verifications within a session."""
    try:
        dao = EmailVerificationDAO(session)
        deleted_count = dao.delete_expired()
        session.commit()

        if deleted_count > 0:
            logger.info(
                "Email verification cleanup: removed %s expired record(s)",
                deleted_count,
            )
        else:
            logger.debug("Email verification cleanup: no expired records to remove")

        return deleted_count

    except Exception as e:
        logger.error("Email verification cleanup failed: %s", e)
        session.rollback()
        raise
