import logging
import os

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import AdminUser
from orchestra.web.api.utils.http_responses import (
    account_frozen,
    admin_not_authorized,
    invalid_api_key,
)
from orchestra.web.api.utils.observability import set_user_context

security = HTTPBearer()
logger = logging.getLogger(__name__)


def auth_api_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    api_key_dao: ApiKeyDAO = Depends(),
) -> None:
    """
    Authenticate an API key.

    :param request_fastapi: FastAPI request object.
    :param credentials: current authorisation credentials.
    :raises HTTPException: when api key is invalid.
    """
    apikey = credentials.credentials

    db_response = api_key_dao.get_user_id_and_mail(apikey)
    if db_response:
        request_fastapi.state.user_id = db_response[0][0]
        request_fastapi.state.user_email = db_response[0][1]

        # Update the user context for logging/tracing
        set_user_context(
            user_id=request_fastapi.state.user_id,
            user_email=request_fastapi.state.user_email,
        )
        return
    raise invalid_api_key


def auth_admin_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    api_key_dao: ApiKeyDAO = Depends(),
    session: Session = Depends(get_db_session),
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
        user_id = api_key_dao.get_user_id_and_mail(admin_key)[0][0]
        admin_user = (
            session.query(AdminUser).filter(AdminUser.user_id == user_id).first()
        )

        if admin_user:
            return
    except Exception as e:
        logger.error(f"Error checking admin user status: {e}")

    # If neither condition is met, raise unauthorized exception
    raise admin_not_authorized


async def check_account_not_frozen(request: Request, users_dao: UsersDAO = Depends()):
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        try:
            if users_dao.is_account_frozen(user_id):
                raise account_frozen
        except Exception as e:
            if e == account_frozen:
                raise account_frozen
            else:
                pass
