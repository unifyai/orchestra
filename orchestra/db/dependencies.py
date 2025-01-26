import logging
from typing import Generator

from sqlalchemy.orm import Session
from starlette.requests import Request

logger = logging.getLogger(__name__)


def get_db_session(request: Request) -> Generator[Session, None, None]:
    """
    Create and get database session.

    :param request: current request.
    :yield: database session.

    Note: The session factory should be configured with:
        - autocommit=False: for explicit transaction management
        - expire_on_commit=False: to allow access to objects after commit

    The session is scoped to the request and automatically closed when done.
    """
    session: Session = request.app.state.db_session_factory()

    try:  # noqa: WPS501
        yield session
    except Exception:
        # Rollback the transaction on any exception
        # This ensures no partial commits are left in case of errors
        session.rollback()
        raise
    finally:
        try:
            session.close()
        except Exception:
            logger.exception("Error while closing database session")
