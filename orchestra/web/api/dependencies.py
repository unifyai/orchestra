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
from orchestra.observability.observability import set_user_context
from orchestra.settings import settings
from orchestra.web.api.utils.http_responses import (
    account_frozen,
    admin_not_authorized,
    invalid_api_key,
    staging_restricted,
)

UNIFY_EMAIL_DOMAIN = "@unify.ai"


def _load_staging_email_allowlist() -> frozenset[str]:
    """Per-address allowlist that augments the @unify.ai domain rule.

    Sourced from ``STAGING_EMAIL_ALLOWLIST`` (comma-separated). Useful for
    e.g. founder personal Gmails or stress-test accounts that legitimately
    need staging access but don't have a @unify.ai address.
    """
    raw = os.environ.get("STAGING_EMAIL_ALLOWLIST", "")
    return frozenset(addr.strip().lower() for addr in raw.split(",") if addr.strip())


STAGING_EMAIL_ALLOWLIST: frozenset[str] = _load_staging_email_allowlist()


def _is_unify_member(email: str | None) -> bool:
    """Return True when ``email`` belongs to a Unify AI member."""
    return bool(email) and email.lower().endswith(UNIFY_EMAIL_DOMAIN)


def is_staging_allowed_email(email: str | None) -> bool:
    """Return True when ``email`` is permitted to use a staging-gated env.

    Allows any @unify.ai address plus any exact match in
    ``STAGING_EMAIL_ALLOWLIST``. Comparison is case-insensitive.
    """
    if not email:
        return False
    if _is_unify_member(email):
        return True
    return email.strip().lower() in STAGING_EMAIL_ALLOWLIST


def enforce_unify_members_only(email: str | None) -> None:
    """
    Block emails that aren't allowed on a staging-gated environment.

    Currently only staging is gated. Shared by the API-key auth dependency,
    the registration middleware, and the verification-token redemption
    endpoint so the gate is consistent end-to-end.
    """
    if settings.is_self_host:
        return
    if settings.is_staging and not is_staging_allowed_email(email):
        raise staging_restricted


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
    expected_admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    if expected_admin_key and secrets.compare_digest(apikey, expected_admin_key):
        request_fastapi.state.user_id = "__system__"
        request_fastapi.state.user_email = "system@builtins.local"
        request_fastapi.state.first_name = "System"
        request_fastapi.state.last_name = "Builtins"
        request_fastapi.state.organization_id = None
        request_fastapi.state.api_key = apikey
        request_fastapi.state.is_system_api_key = True
        set_user_context(
            user_id=request_fastapi.state.user_id,
            user_email=request_fastapi.state.user_email,
            first_name=request_fastapi.state.first_name,
            last_name=request_fastapi.state.last_name,
        )
        return

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

            # Gate restricted environments (e.g. staging) to Unify members.
            # Runs after we know the user so the 403 is meaningful in logs.
            enforce_unify_members_only(request_fastapi.state.user_email)

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


def require_unify_staff(request_fastapi: Request) -> None:
    """Allow only internal Unify staff callers on a user-authenticated route.

    Intended to gate value-minting operations (e.g. credit-grant links) that
    must never be callable by an ordinary customer even though the customer
    holds a valid user API key. Ownership scoping is insufficient here because
    every customer owns their own assistant/account.

    Runs after :func:`auth_api_key` (which populates ``request.state``). Permits:
    the platform admin/system key, any ``@unify.ai`` member, or a registered
    ``AdminUser``. Raises 403 otherwise.
    """
    state = getattr(request_fastapi, "state", None)
    if state is not None and getattr(state, "is_system_api_key", False):
        return

    email = getattr(state, "user_email", None)
    if _is_unify_member(email):
        return

    user_id = getattr(state, "user_id", None)
    if user_id:
        try:
            with _ro_session() as session:
                is_admin = (
                    session.query(AdminUser)
                    .filter(AdminUser.user_id == user_id)
                    .first()
                    is not None
                )
                if is_admin:
                    return
        except Exception as e:
            logger.error(f"Error checking Unify staff status: {e}")

    raise admin_not_authorized


_FREEZE_EXEMPT_PATHS = frozenset(
    {
        "/v0/billing/account-info",
        "/v0/billing/portal-session",
        # The card-gate remediation loop must survive the freeze: the
        # console reads the gate state to explain the lock, and the
        # trial Checkout is the self-serve path out of a
        # ``card_required`` suspension (completion reinstates the
        # account via the checkout.session.completed webhook).
        "/v0/billing/access-gate",
        "/v0/billing/trial-checkout",
        # Account setup must survive the freeze for the same reason.
        # The console pins any user whose onboarding is incomplete to
        # /login/onboarding, so a user frozen mid-signup can neither
        # finish onboarding nor reach the card page that would lift the
        # freeze. These endpoints only move onboarding state — they
        # consume no credits and confer no platform access — so the
        # gate lands where it belongs: on the first real request after
        # onboarding completes.
        "/v0/user/onboarding",
        "/v0/user/onboarding-status",
    },
)


def check_account_not_frozen(request: Request):
    """
    Check if the relevant billing account is frozen (dispute / fraud).

    Only SUSPENDED and CLOSED accounts are hard-blocked.  Balance-based
    enforcement for billable actions is handled per-handler (credits
    checks) and by Unity's spending-limit hook — not here.

    Read-only billing endpoints (account-info, portal-session) and the
    onboarding progress endpoints are exempted so the frontend can
    display account-status banners, let users finish account setup, and
    let them manage payment methods to resolve suspensions.

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
