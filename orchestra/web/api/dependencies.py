import logging
import os
import secrets
from contextlib import contextmanager

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.models.orchestra_models import AdminUser
from orchestra.web.api.utils.http_responses import (
    account_frozen,
    admin_not_authorized,
    invalid_api_key,
)
from orchestra.web.api.utils.observability import set_user_context

security = HTTPBearer()
logger = logging.getLogger(__name__)


# READ‑ONLY session for auth dependencies
@contextmanager
def _ro_session(autoflush=False, expire_on_commit=False):
    from orchestra.web.lifetime import get_engine

    engine = get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        SessionLocal = sessionmaker(
            bind=conn,
            autoflush=autoflush,
            expire_on_commit=expire_on_commit,
        )
        session: Session = SessionLocal()
        try:
            yield session
        finally:
            session.close()


def auth_api_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    """
    Authenticate an API key.

    :param request_fastapi: FastAPI request object.
    :param credentials: current authorisation credentials.
    :raises HTTPException: when api key is invalid.
    """
    apikey = credentials.credentials

    with _ro_session() as session:  # <-- opens & closes inside
        api_key_dao = ApiKeyDAO(session)
        db_response = api_key_dao.get_user_id_and_mail(apikey)

        if db_response:
            request_fastapi.state.user_id = db_response[0][0]
            request_fastapi.state.user_email = db_response[0][1]
            request_fastapi.state.first_name = db_response[0][2]
            request_fastapi.state.last_name = db_response[0][3]
            request_fastapi.state.organization_id = db_response[0][4]
            request_fastapi.state.api_key = apikey

            # Update the user context for logging/tracing
            set_user_context(
                user_id=request_fastapi.state.user_id,
                user_email=request_fastapi.state.user_email,
                first_name=request_fastapi.state.first_name,
                last_name=request_fastapi.state.last_name,
            )
            return
    raise invalid_api_key


def auth_admin_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    """
    Authenticate an admin key.

    :param request_fastapi: FastAPI request object.
    :param credentials: current authorisation credentials.
    :param db: Database session.
    :raises HTTPException: when admin key is invalid.
    """
    admin_key = credentials.credentials

    expected_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    if expected_key and secrets.compare_digest(admin_key, expected_key):
        return

    # Verify Cloud Scheduler OIDC tokens with full JWT signature verification
    scheduler_sa = os.environ.get("CLOUD_SCHEDULER_SERVICE_ACCOUNT")
    if scheduler_sa:
        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token

            claims = id_token.verify_oauth2_token(
                admin_key,
                google_requests.Request(),
            )
            if claims.get("email") == scheduler_sa:
                return
        except Exception:
            pass

    # If not, check if the user is an admin user in the database
    try:
        with _ro_session() as session:
            dao = ApiKeyDAO(session)
            row = dao.get_user_id_and_mail(admin_key)
            if row:
                user_id = row[0][0]
                is_admin = (
                    session.query(AdminUser)
                    .filter(AdminUser.user_id == user_id)
                    .first()
                    is not None
                )
                if is_admin:
                    return
    except Exception as e:
        logger.error(f"Error checking admin user status: {e}")

    # If neither condition is met, raise unauthorized exception
    raise admin_not_authorized


_FREEZE_EXEMPT_PATHS = frozenset(
    {
        "/v0/billing/account-info",
        "/v0/billing/portal-session",
    },
)


def check_account_not_frozen(request: Request):
    """
    Check if the relevant billing account is frozen (dispute / fraud).

    Only SUSPENDED and CLOSED accounts are hard-blocked.  Balance-based
    enforcement for billable actions is handled per-handler (credits
    checks) and by Unity's spending-limit hook — not here.

    Read-only billing endpoints (account-info, portal-session) are
    exempted so the frontend can display account-status banners and
    allow users to manage their payment methods to resolve suspensions.

    Fails closed: if the DB check itself errors, the request is blocked
    to prevent suspended accounts from exploiting transient DB issues.
    """
    if request.url.path in _FREEZE_EXEMPT_PATHS:
        return

    user_id = getattr(request.state, "user_id", None)
    organization_id = getattr(request.state, "organization_id", None)
    if not user_id:
        return

    try:
        with _ro_session() as session:
            ba_dao = BillingAccountDAO(session)
            ba = ba_dao.resolve(user_id, organization_id)
            if ba is None:
                return  # No billing account → allow through

            if ba.account_status in ("SUSPENDED", "CLOSED"):
                raise account_frozen

    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to check account frozen status for user %s — "
            "blocking request (fail-closed)",
            user_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Unable to verify account status. Please try again.",
        )
