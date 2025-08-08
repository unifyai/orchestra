from typing import Any, List, Optional, Type

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from orchestra.settings import settings


def create_database(worker_id=None) -> None:
    """Create a database."""
    url = str(settings.db_url.with_path("/postgres"))
    datname = settings.db_base
    if worker_id:
        url = url.replace("orchestra_test", f"orchestra_test_{worker_id}")
        datname += f"_{worker_id}"
    db_url = make_url(url)
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")

    with engine.connect() as conn:
        database_existance = conn.execute(
            text(
                f"SELECT 1 FROM pg_database WHERE datname='{datname}'",  # noqa: E501, S608
            ),
        )
        database_exists = database_existance.scalar() == 1

    if database_exists:
        drop_database(worker_id)

    with engine.connect() as conn:  # noqa: WPS440
        conn.execute(
            text(
                f'CREATE DATABASE "{datname}" ENCODING "utf8" TEMPLATE template1',  # noqa: E501
            ),
        )


def drop_database(worker_id=None) -> None:
    """Drop current database."""
    url = str(settings.db_url.with_path("/postgres"))
    datname = settings.db_base
    if worker_id:
        url = url.replace("orchestra_test", f"orchestra_test_{worker_id}")
        datname += f"_{worker_id}"
    db_url = make_url(url)
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")

    with engine.connect() as conn:
        disc_users = (
            "SELECT pg_terminate_backend(pg_stat_activity.pid) "  # noqa: S608
            "FROM pg_stat_activity "
            f"WHERE pg_stat_activity.datname = '{datname}' "
            "AND pid <> pg_backend_pid();"
        )
        conn.execute(text(disc_users))
        conn.execute(text(f'DROP DATABASE "{datname}"'))


def get_next_order_value(
    session: Session,
    model_class: Type,
    order: Optional[int] = None,
    where_conditions: Optional[List[Any]] = None,
) -> int:
    """
    Get the next order value for a model, either using the provided order
    or auto-incrementing based on the current maximum order value.

    Args:
        session: SQLAlchemy session
        model_class: The model class (e.g., Project, Interface, Tab)
        order: Explicit order value to use, or None for auto-increment
        where_conditions: List of SQLAlchemy where conditions to apply when finding max order

    Returns:
        The order value to use (either the provided order or next auto-increment value)
    """
    if order is not None:
        return order

    # Build query to find maximum order value
    max_order_query = select(func.max(model_class.order))

    # Apply any where conditions
    if where_conditions:
        for condition in where_conditions:
            max_order_query = max_order_query.where(condition)

    # Execute query and calculate next order value
    max_order = session.execute(max_order_query).scalar_one()
    return (max_order or -1) + 1
