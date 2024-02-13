import os
from typing import Any, Generator

import pytest
from fastapi import FastAPI
from httpx import Client  # TODO
from sqlalchemy import text
from sqlalchemy.orm import (
    Engine,
    Session,
    sessionmaker,
    create_engine,
)

from orchestra.db.dependencies import get_db_session
from orchestra.db.utils import create_database, drop_database
from orchestra.settings import settings
from orchestra.web.application import get_app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """
    Backend for anyio pytest plugin.

    :return: backend name.
    """
    return "asyncio"


@pytest.fixture(scope="session")
def _engine() -> Generator[Engine, None]:
    """
    Create engine and databases.

    :yield: new engine.
    """
    from orchestra.db.meta import meta  # noqa: WPS433
    from orchestra.db.models import load_all_models  # noqa: WPS433

    load_all_models()

    create_database()

    engine = create_engine(str(settings.db_url))
    with engine.begin() as conn:
        conn.run_sync(meta.create_all)
        user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
        insert_user = text(
            f"INSERT INTO users VALUES ('{user_id}', 10);",  # noqa: S608
        )
        conn.execute(insert_user)

    try:
        yield engine
    finally:
        engine.dispose()
        drop_database()


@pytest.fixture
def dbsession(
    _engine: Engine,
) -> Generator[Session, None]:
    """
    Get session to database.

    Fixture that returns a SQLAlchemy session with a SAVEPOINT, and the rollback to it
    after the test completes.

    :param _engine: current engine.
    :yields: session.
    """
    connection = _engine.connect()
    trans = connection.begin()

    session_maker = sessionmaker(connection, expire_on_commit=False)
    session = session_maker()

    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


@pytest.fixture
def fastapi_app(
    dbsession: Session,
) -> FastAPI:
    """
    Fixture for creating FastAPI app.

    :return: fastapi app with mocked dependencies.
    """
    application = get_app()
    application.dependency_overrides[get_db_session] = lambda: dbsession
    return application  # noqa: WPS331


@pytest.fixture
def client(
    fastapi_app: FastAPI,
    anyio_backend: Any,
) -> Generator[Client, None]:
    """
    Fixture that creates client for requesting server.

    :param fastapi_app: the application.
    :yield: client for the app.
    """
    with Client(
        app=fastapi_app, base_url="http://test"
    ) as ac:  # TODO: See if this needs to be asyn
        yield ac
