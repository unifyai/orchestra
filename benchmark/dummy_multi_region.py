import asyncio
import logging
import os
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from orchestra.db.models.orchestra_models import (  # noqa: WPS235
    Endpoint,
    Model,
    Provider,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


async def retrieve_all_endpoints(async_session: sessionmaker) -> List[Dict]:
    # noqa: DAR101, DAR201
    """Retrieves a list of all the endpoints in the db.

    Args:
        async_session (sessionmaker): Async SQLAlchemy session maker.

    Returns:
        List[Dict]: List of endpoints dictionaries with keys "id", "provider"
        and "model".
    """
    async with async_session() as session:
        stmt = (
            select(Endpoint, Model, Provider)
            .join(Model)
            .join(Provider)
            .where(Model.active == True)
        )
        results = await session.execute(stmt)
    endpoints = []
    for result in results.all():
        endpoints.append(
            {
                "id": result.Endpoint.id,
                "provider": result.Provider.name,
                "model": result.Model.mdl_code,
            },
        )
    return endpoints


def create_db_session() -> sessionmaker:  # noqa: WPS210
    # noqa: DAR201
    """Creates an async db session.

    If ORCHESTRA_<> env vars are not defined, it defaults to
    orchestra:orchestra@localhost:5432/orchestra.

    Returns:
        sessionmaker: Async SQLAlchemy session maker.
    """
    user = os.getenv("ORCHESTRA_DB_USER", "orchestra")
    password = os.getenv("ORCHESTRA_DB_PASS", "orchestra")
    host = os.getenv("ORCHESTRA_DB_HOST", "localhost")
    port = os.getenv("ORCHESTRA_DB_PORT", "5432")
    db_name = os.getenv("ORCHESTRA_DB_BASE", "orchestra")
    db_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"  # noqa: WPS221, E501
    # TODO: logger.info(db_url)
    engine = create_async_engine(db_url)
    return sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def main():  # noqa: WPS210
    # Get region information from the GCP instance
    region = os.getenv("BENCHMARK_REGION")
    if not region:
        raise ValueError("BENCHMARK_REGION env was not declared")
    logger.info(f"Running benchmark in '{region}' region.")

    # Initialise db engine
    async_db_session = create_db_session()

    # Get list of endpoints in our db
    endpoints = await retrieve_all_endpoints(async_db_session)
    logger.info(f"Found {len(endpoints)} endpoints where Model is active in the db.")


if __name__ == "__main__":
    asyncio.run(main())
