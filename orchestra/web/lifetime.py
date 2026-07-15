import logging
import os
from typing import Callable

from fastapi import FastAPI
from google.cloud import aiplatform
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import orchestra.db.models.coordinator_voice  # noqa: F401 — register ORM listeners
from orchestra.db.dependencies import register_db_listeners
from orchestra.observability import otel_setup
from orchestra.observability.inactivity_shutdown import (
    start_inactivity_monitor,
    stop_inactivity_monitor,
)
from orchestra.observability.otel_setup import stop_opentelemetry
from orchestra.settings import settings
from orchestra.web.api.utils.resource_limits_instrumentation import instrument_db_pool

logger = logging.getLogger(__name__)

# Global variable to store the engine instance
_engine = None

# Cap abandoned transactions so they cannot hold ACCESS SHARE on hot tables
# (e.g. assistants) long enough to queue ACCESS EXCLUSIVE DDL and wedge reads.
_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS = 60_000


def _configure_session_timeouts(dbapi_connection, connection_record) -> None:
    """Apply per-connection Postgres timeouts for both psycopg2 and pg8000."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(
            f"SET idle_in_transaction_session_timeout = "
            f"'{_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS}ms'",
        )
    finally:
        cursor.close()


def create_database_engine() -> Engine:
    """Create the configured SQLAlchemy engine with shared runtime safeguards."""

    # Use standard SQLAlchemy connection if not using Cloud SQL
    if not settings.use_cloud_sql:
        engine = create_engine(
            str(settings.db_url),
            echo=settings.db_echo,
            pool_size=50,
            max_overflow=100,  # noqa: WPS432, E501
            pool_pre_ping=True,
        )
    else:
        # Use Cloud SQL connector for GCP deployment
        from google.cloud.sql.connector import Connector

        # Get connection details from environment or settings
        instance_connection_name = os.environ.get(
            "INSTANCE_CONNECTION_NAME",
            getattr(settings, "cloud_sql_instance", ""),
        )
        db_user = os.environ.get("DB_USER", settings.db_user)
        db_pass = os.environ.get("DB_PASS", settings.db_pass)
        db_name = os.environ.get("DB_NAME", settings.db_base)

        # Validate required connection information
        if not instance_connection_name:
            raise ValueError("Missing Cloud SQL instance connection name")

        connector = Connector()

        def get_conn():
            return connector.connect(
                instance_connection_name,
                "pg8000",
                user=db_user,
                password=db_pass,
                db=db_name,
            )

        engine = create_engine(
            "postgresql+pg8000://",
            creator=get_conn,
        )

    event.listen(engine, "connect", _configure_session_timeouts)

    # Instrument the connection pool for bottleneck detection
    instrument_db_pool(engine)
    return engine


def _setup_db(app: FastAPI) -> None:  # pragma: no cover
    """
    Creates connection to the database.

    This function creates SQLAlchemy engine instance,
    session_factory for creating sessions
    and stores them in the application's state property.

    :param app: fastAPI application.
    """
    global _engine

    engine = create_database_engine()
    session_factory = sessionmaker(
        engine,
        expire_on_commit=False,
    )

    # Store engine and session_factory in app state
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory

    # Store engine in global variable for access from other modules
    _engine = engine


def get_engine():
    """
    Get the SQLAlchemy engine.

    This function returns the global engine instance that was created
    during application startup.

    Returns:
        The SQLAlchemy engine instance.
    """
    global _engine

    if _engine is None:
        raise RuntimeError("Database engine not initialized")

    return _engine


def setup_opentelemetry(app: FastAPI) -> None:
    """Set up the kernel OTel stack and SQLAlchemy instrumentation."""

    otel_setup.setup_opentelemetry(app)

    if not settings.otel_enabled or not otel_setup._otel_tracer_provider_initialized:
        return

    try:
        SQLAlchemyInstrumentor().uninstrument()
    except Exception as e:
        logger.debug(f"Failed to uninstrument SQLAlchemy: {e}")


def setup_observability(app: FastAPI) -> None:  # pragma: no cover
    """
    Initializes the full observability stack including OpenTelemetry,
    Prometheus metrics, Loki logging configuration, and database query tracking.

    :param app: current application.
    """
    # # Setup logging with JSON formatting and Loki integration first
    # log_level = getattr(settings, "log_level", "INFO")
    # try:
    #     setup_logging(log_level)
    # except Exception as e:
    #     logger.error(f"Error setting up logging: {e}")
    #     # Continue with basic logging if advanced setup fails
    #     logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO))

    # # Add this before OpenTelemetry setup
    # if settings.grafana_url:
    #     logger.info(f"Grafana dashboard available at {settings.grafana_url}")

    # Setup OpenTelemetry for distributed tracing
    try:
        setup_opentelemetry(app)
    except Exception as e:
        logger.error(f"Failed to setup OpenTelemetry: {e}")
        logger.info("Continuing without distributed tracing")

    # Setup SQLAlchemy instrumentation for query tracking
    # Only register DB listeners if the engine is already initialized
    if hasattr(app.state, "db_engine") and app.state.db_engine is not None:
        try:
            register_db_listeners(app.state.db_engine)
        except Exception as e:
            logger.error(f"Failed to register DB listeners: {e}")

    logger.info("Observability stack setup completed")


def register_startup_event(
    app: FastAPI,
) -> Callable[[], None]:  # pragma: no cover
    """
    Actions to run on application startup.

    This function uses fastAPI app to store data
    in the state, such as db_engine.

    :param app: the fastAPI application.
    :return: function that actually performs actions.
    """

    @app.on_event("startup")
    def _startup() -> None:  # noqa: WPS430
        settings.assert_unique_validation_mode_safe()
        app.middleware_stack = None
        _setup_db(app)
        setup_observability(app)
        aiplatform.init(
            project=settings.gcp_project,
            location=settings.gcp_location,
        )
        app.middleware_stack = app.build_middleware_stack()
        start_inactivity_monitor()

    return _startup


def register_shutdown_event(
    app: FastAPI,
) -> Callable[[], None]:  # pragma: no cover
    """
    Actions to run on application's shutdown.

    :param app: fastAPI application.
    :return: function that actually performs actions.
    """

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # noqa: WPS430
        from orchestra.web.api.utils.http_client import close_async_client

        await close_async_client()
        stop_inactivity_monitor()
        app.state.db_engine.dispose()
        stop_opentelemetry(app)

    return _shutdown
