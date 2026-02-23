import logging
import os
from contextlib import contextmanager

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.models.orchestra_models import (
    AdminUser,
    BillingAccount,
    Organization,
    User,
)
from orchestra.web.api.utils.http_responses import (
    account_frozen,
    account_suspended,
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

    # First check if the provided key matches the admin key from environment
    if admin_key == os.environ["ORCHESTRA_ADMIN_KEY"]:
        return

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


def _is_billing_account_frozen(session: Session, ba_id: int) -> bool:
    """Check if a BillingAccount is PAST_DUE, SUSPENDED, or CLOSED."""
    ba = session.query(BillingAccount).filter(BillingAccount.id == ba_id).first()
    if ba is None:
        return False
    if ba.account_status in ("SUSPENDED", "CLOSED"):
        return True
    if ba.account_status == "PAST_DUE":
        raise account_suspended
    return False


def check_account_not_frozen(request: Request):
    """
    Check if the relevant billing account is frozen or suspended.

    For personal API keys: checks the user's BillingAccount.
    For org API keys: checks the organization's BillingAccount.
    """
    user_id = getattr(request.state, "user_id", None)
    organization_id = getattr(request.state, "organization_id", None)
    if not user_id:
        return

    try:
        with _ro_session() as session:
            # If request is in org context, check the org's billing account
            if organization_id:
                org = (
                    session.query(Organization)
                    .filter(Organization.id == organization_id)
                    .first()
                )
                if org and org.billing_account_id:
                    if _is_billing_account_frozen(session, org.billing_account_id):
                        raise account_frozen
                return

            # Personal context — check user's billing account
            user = session.query(User).filter(User.id == user_id).first()
            if user and user.billing_account_id:
                if _is_billing_account_frozen(session, user.billing_account_id):
                    raise account_frozen

    except Exception as e:
        if e == account_frozen or e == account_suspended:
            raise
        # If there's any other error, allow the request to proceed
        # rather than blocking legitimate users
