import hashlib
import logging
from typing import Generator

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

logger = logging.getLogger(__name__)


def get_db_session(request: Request) -> Generator[Session, None, None]:
    """
    Create and get database session.

    :param request: current request.
    :yield: database session.
    """
    session: Session = request.app.state.db_session_factory()

    try:  # noqa: WPS501
        yield session
    # except Exception as e:
    #     digest = hashlib.shake_256(str(e).encode()).digest(4).hex()
    #     logger.error(f"Digest {digest}: {e}")
    #     raise HTTPException(
    #         status_code=500,  # noqa: WPS432
    #         detail=f"Internal Server Error. Digest: {digest}",
    #     )
    finally:
        session.commit()
        session.close()
