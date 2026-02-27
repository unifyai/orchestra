import logging
import os
from typing import Callable

import starlette.routing
from fastapi import FastAPI
from google.cloud import aiplatform
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.openai import OpenAIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import (
    DEPLOYMENT_ENVIRONMENT,
    SERVICE_NAME,
    TELEMETRY_SDK_LANGUAGE,
    Resource,
)
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.trace import get_tracer_provider, set_tracer_provider
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dependencies import register_db_listeners
from orchestra.settings import settings
from orchestra.web.api.utils.inactivity_shutdown import (
    start_inactivity_monitor,
    stop_inactivity_monitor,
)
from orchestra.web.api.utils.resource_limits_instrumentation import instrument_db_pool

logger = logging.getLogger(__name__)

# Global variable to store the engine instance
_engine = None

# Track if OTel TracerProvider has been initialized (for idempotent setup)
# This allows setup_opentelemetry to be called multiple times (e.g., in tests)
# without recreating the TracerProvider each time
_otel_tracer_provider_initialized = False


def _setup_db(app: FastAPI) -> None:  # pragma: no cover
    """
    Creates connection to the database.

    This function creates SQLAlchemy engine instance,
    session_factory for creating sessions
    and stores them in the application's state property.

    :param app: fastAPI application.
    """
    global _engine

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

    session_factory = sessionmaker(
        engine,
        expire_on_commit=False,
    )

    # Instrument the connection pool for bottleneck detection
    instrument_db_pool(engine)

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


def _create_tracer_provider() -> TracerProvider:
    """
    Create and configure a TracerProvider with all exporters.

    This is separated from setup_opentelemetry to allow the TracerProvider
    to be created once and reused across multiple app instances (e.g., in tests).
    """
    resource = Resource.create(
        {
            SERVICE_NAME: "orchestra",
            TELEMETRY_SDK_LANGUAGE: "python",
            DEPLOYMENT_ENVIRONMENT: settings.environment,
        },
    )

    tracer_provider = TracerProvider(resource=resource)

    def _add_processor(proc: SpanProcessor) -> None:
        """Wrap processor with filtering if exclude patterns are configured."""
        if settings.otel_exclude_patterns:
            from orchestra.web.api.utils.filtering_span_processor import (
                FilteringSpanProcessor,
            )

            proc = FilteringSpanProcessor(proc, settings.otel_exclude_patterns)
        tracer_provider.add_span_processor(proc)

    # Add OTLP exporter if configured
    if settings.otel_endpoint:
        try:
            _add_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=settings.otel_endpoint,
                        insecure=not settings.otel_secure,
                        timeout=5,
                    ),
                ),
            )
            logger.info(
                f"Configured OTLP exporter at {settings.otel_endpoint}",
            )
        except Exception as e:
            logger.warning(f"Failed to configure OTLP exporter: {e}")

    # Add Tempo exporter if configured
    if settings.tempo_url:
        try:
            if ":4318" in settings.tempo_url:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as HTTPSpanExporter,
                )

                tempo_endpoint = f"{settings.tempo_url}/v1/traces"
                tempo_exporter = HTTPSpanExporter(
                    endpoint=tempo_endpoint,
                    timeout=5,
                )
                logger.info(f"Configured Tempo HTTP exporter at {tempo_endpoint}")
            elif ":4317" in settings.tempo_url:
                tempo_exporter = OTLPSpanExporter(
                    endpoint=settings.tempo_url,
                    insecure=True,
                    timeout=5,
                )
                logger.info(f"Configured Tempo gRPC exporter at {settings.tempo_url}")
            else:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as HTTPSpanExporter,
                )

                tempo_endpoint = f"{settings.tempo_url}/v1/traces"
                tempo_exporter = HTTPSpanExporter(
                    endpoint=tempo_endpoint,
                    timeout=5,
                )
                logger.info(f"Configured Tempo HTTP exporter at {tempo_endpoint}")

            _add_processor(BatchSpanProcessor(tempo_exporter))

        except Exception as e:
            logger.warning(f"Failed to configure Tempo exporter: {e}")
            logger.warning(
                "Continuing without Tempo tracing. "
                "Make sure Tempo is running at the configured URL.",
            )

    # Add JSONL exporter for unified traces with Unity (ORCHESTRA_OTEL_LOG_DIR)
    if settings.log_enabled and settings.otel_log_dir:
        try:
            from orchestra.web.api.utils.file_trace_exporter import JsonlSpanExporter

            jsonl_exporter = JsonlSpanExporter(
                settings.otel_log_dir,
                service_name="orchestra",
            )
            _add_processor(SimpleSpanProcessor(jsonl_exporter))
            logger.info(
                f"Configured JSONL span exporter at {settings.otel_log_dir}",
            )
        except Exception as e:
            logger.warning(f"Failed to configure JSONL span exporter: {e}")

    # Add per-request JSON exporter for Orchestra-centric debugging (ORCHESTRA_LOG_DIR)
    if settings.log_enabled and settings.log_dir:
        try:
            from orchestra.web.api.utils.file_trace_exporter import FileSpanExporter

            file_exporter = FileSpanExporter(settings.log_dir)
            _add_processor(BatchSpanProcessor(file_exporter))
            logger.info(
                f"Configured per-request JSON exporter at {settings.log_dir}",
            )
        except Exception as e:
            logger.warning(f"Failed to configure per-request JSON exporter: {e}")

    return tracer_provider


def setup_opentelemetry(app: FastAPI) -> None:
    """
    Enables opentelemetry instrumentation.

    This function is idempotent: the TracerProvider and global library instrumentation
    (OpenAI, httpx) are set up once, while per-app instrumentation (FastAPI, SQLAlchemy)
    happens on each call. This supports both production (single app) and tests (multiple
    app instances sharing the same TracerProvider).

    :param app: current application.
    """
    global _otel_tracer_provider_initialized

    # Check master switch first
    if not settings.otel_enabled:
        return

    # Enable tracing if any backend is configured (OTLP, Tempo, or local file)
    if (
        not settings.otel_endpoint
        and not settings.tempo_url
        and not settings.log_dir
        and not settings.otel_log_dir
    ):
        return

    # Create TracerProvider once and set as global (idempotent)
    if not _otel_tracer_provider_initialized:
        tracer_provider = _create_tracer_provider()
        set_tracer_provider(tracer_provider=tracer_provider)

        # Instrument global libraries once
        OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
        logger.info("Instrumented OpenAI client for tracing")

        HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
        logger.info("Instrumented httpx client for HTTP-level tracing")

        _otel_tracer_provider_initialized = True
        logger.info("OTel TracerProvider initialized")
    else:
        logger.debug("OTel TracerProvider already initialized, reusing")

    # Get the current tracer provider (either just created or existing)
    tracer_provider = get_tracer_provider()

    # Instrument per-app components (FastAPI and SQLAlchemy)
    # These are safe to call multiple times for different app/engine instances
    _exclude_names = [
        "health_check",
        "openapi",
        "swagger_ui_html",
        "swagger_ui_redirect",
        "redoc_html",
        "metrics",
    ]
    excluded_endpoints = []
    for name in _exclude_names:
        try:
            excluded_endpoints.append(str(app.url_path_for(name)))
        except starlette.routing.NoMatchFound:
            pass

    FastAPIInstrumentor().instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls=",".join(excluded_endpoints),
    )

    if hasattr(app.state, "db_engine") and app.state.db_engine is not None:
        SQLAlchemyInstrumentor().instrument(
            tracer_provider=tracer_provider,
            engine=app.state.db_engine,
        )


def flush_opentelemetry(timeout_millis: int = 5000) -> None:
    """
    Flush all pending traces to ensure they are written to exporters.

    Call this before process exit or test teardown to ensure all traces are captured.

    :param timeout_millis: Maximum time to wait for flush to complete.
    """
    if not settings.otel_enabled or not _otel_tracer_provider_initialized:
        return

    tracer_provider = get_tracer_provider()
    if hasattr(tracer_provider, "force_flush"):
        try:
            tracer_provider.force_flush(timeout_millis=timeout_millis)
            logger.debug("Flushed OTel traces")
        except Exception as e:
            logger.warning(f"Failed to flush OTel traces: {e}")


def stop_opentelemetry(app: FastAPI) -> None:
    """
    Disables opentelemetry instrumentation for a specific app.

    :param app: current application.
    """
    if not settings.otel_enabled:
        return

    try:
        FastAPIInstrumentor().uninstrument_app(app)
    except Exception as e:
        logger.debug(f"Failed to uninstrument FastAPI app: {e}")

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
            register_db_listeners()
        except Exception as e:
            logger.error(f"Failed to register DB listeners: {e}")

    logger.info("Observability stack setup completed")


def ensure_production_traffic_project_exists(app: FastAPI):
    """Ensures a special admin organization and the 'Production Traffic' project exist, and assigns all AdminUser records as admin members."""
    from orchestra.db.dao.organization_dao import OrganizationDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO
    from orchestra.db.models.orchestra_models import AdminUser

    session = app.state.db_session_factory()

    try:
        # 1. Find or create 'Admin Organization'
        org_dao = OrganizationDAO(session=session)
        ORGANIZATION_ID = settings.orchestra_organization_id
        ORGANIZATION_NAME = settings.orchestra_organization_name
        OWNER_ID = settings.orchestra_owner_id
        PROJ_NAME = settings.orchestra_prod_traffic_name
        logging.info(
            f"Ensuring {ORGANIZATION_NAME} with owner {OWNER_ID} and {PROJ_NAME} exist",
        )
        orgs = org_dao.filter(id=ORGANIZATION_ID)
        if orgs:
            admin_org = orgs[0][0]
        else:
            org_dao.create(name=ORGANIZATION_NAME, owner_id=OWNER_ID)
            session.commit()
            admin_org = org_dao.filter(name=ORGANIZATION_NAME)[0][0]

        # 2. Ensure all AdminUser records are added as admin members
        org_member_dao = OrganizationMemberDAO(session=session)
        admin_users = session.query(AdminUser).all()
        for admin_user in admin_users:
            logging.info(
                f"Ensuring {admin_user.user_id} is added to {ORGANIZATION_NAME}",
            )
            existing_memberships = org_member_dao.filter(
                user_id=admin_user.user_id,
                organization_id=admin_org.id,
            )
            if not existing_memberships:
                logging.info(f"Adding {admin_user.user_id} to {ORGANIZATION_NAME}")
                org_member_dao.create(
                    user_id=admin_user.user_id,
                    organization_id=admin_org.id,
                    level="admin",
                )

        # 3. Create the 'Production Traffic' project if it doesn't already exist
        organization_member_dao = OrganizationMemberDAO(session=session)
        context_dao = ContextDAO(session=session)
        project_dao = ProjectDAO(
            session=session,
            organization_member_dao=organization_member_dao,
            context_dao=context_dao,
        )
        existing_project = project_dao.filter(
            organization_id=admin_org.id,
            name=PROJ_NAME,
        )
        if not existing_project:
            logging.info(f"Creating {PROJ_NAME} in {ORGANIZATION_NAME}")
            project_dao.create(
                name=PROJ_NAME,
                organization_id=admin_org.id,
            )
            session.commit()
            existing_project = project_dao.filter(
                organization_id=admin_org.id,
                name=PROJ_NAME,
            )

        logging.info(
            f"Production Traffic project {PROJ_NAME} created in {ORGANIZATION_NAME}",
        )
    except Exception as e:
        logging.error(f"Error creating Production Traffic project: {e}")
        session.rollback()
    finally:
        session.close()


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
        app.middleware_stack = None
        _setup_db(app)
        setup_observability(app)
        aiplatform.init(
            project=settings.gcp_project,
            location=settings.gcp_location,
        )
        app.middleware_stack = app.build_middleware_stack()
        # ensure_production_traffic_project_exists(app)
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
    def _shutdown() -> None:  # noqa: WPS430
        stop_inactivity_monitor()
        app.state.db_engine.dispose()
        stop_opentelemetry(app)

    return _shutdown
