import os
from typing import Any, AsyncGenerator, Generator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

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
def _engine(worker_id) -> Generator[Engine, None, None]:
    """
    Create engine and databases.

    :yield: new engine.
    """
    from orchestra.db.meta import meta  # noqa: WPS433
    from orchestra.db.models import load_all_models  # noqa: WPS433

    load_all_models()

    create_database(worker_id)

    url = str(settings.db_url)
    # If using xdist, the testing database (orchestra_test) needs to be
    # instantiated for every thread
    if worker_id:
        url = url.replace("orchestra_test", f"orchestra_test_{worker_id}")
    engine = create_engine(url)
    meta.create_all(engine)
    with engine.begin() as conn:
        user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
        with open("orchestra/tests/seeding.sql") as file:
            conn.execute(text(file.read()), {"user_id": user_id})

    try:
        yield engine
    finally:
        engine.dispose()
        drop_database(worker_id)


@pytest.fixture
def dbsession(
    _engine: Engine,
) -> Generator[Session, None, None]:
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
async def client(
    fastapi_app: FastAPI,
    anyio_backend: Any,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Fixture that creates client for requesting server.

    :param fastapi_app: the application.
    :yield: client for the app.
    """
    async with AsyncClient(app=fastapi_app, base_url="http://test") as ac:
        yield ac
