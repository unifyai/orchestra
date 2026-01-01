import logging
import os
from typing import Callable

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
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import set_tracer_provider
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dependencies import register_db_listeners
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# Global variable to store the engine instance
_engine = None


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


def setup_opentelemetry(app: FastAPI) -> None:  # pragma: no cover
    """
    Enables opentelemetry instrumentation.

    :param app: current application.
    """
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

    # Create resource with service information
    resource = Resource.create(
        {
            SERVICE_NAME: "orchestra",
            TELEMETRY_SDK_LANGUAGE: "python",
            DEPLOYMENT_ENVIRONMENT: settings.environment,
        },
    )

    tracer_provider = TracerProvider(resource=resource)

    # Add OTLP exporter if configured
    if settings.otel_endpoint:
        try:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=settings.otel_endpoint,
                        insecure=not settings.otel_secure,
                        timeout=5,  # Add timeout to prevent hanging
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
            # Determine if we're using HTTP or gRPC based on the port
            if ":4318" in settings.tempo_url:
                # Use HTTP exporter for port 4318
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as HTTPSpanExporter,
                )

                # For HTTP, use the /v1/traces endpoint
                tempo_endpoint = f"{settings.tempo_url}/v1/traces"
                tempo_exporter = HTTPSpanExporter(
                    endpoint=tempo_endpoint,
                    timeout=5,  # Add timeout to prevent hanging
                )
                logger.info(f"Configured Tempo HTTP exporter at {tempo_endpoint}")
            elif ":4317" in settings.tempo_url:
                # Use gRPC exporter for port 4317
                tempo_exporter = OTLPSpanExporter(
                    endpoint=settings.tempo_url,
                    insecure=True,  # Most Tempo deployments don't use TLS internally
                    timeout=5,  # Add timeout to prevent hanging
                )
                logger.info(f"Configured Tempo gRPC exporter at {settings.tempo_url}")
            else:
                # Default to HTTP if port not specified
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as HTTPSpanExporter,
                )

                # For HTTP, use the /v1/traces endpoint
                tempo_endpoint = f"{settings.tempo_url}/v1/traces"
                tempo_exporter = HTTPSpanExporter(
                    endpoint=tempo_endpoint,
                    timeout=5,  # Add timeout to prevent hanging
                )
                logger.info(f"Configured Tempo HTTP exporter at {tempo_endpoint}")

            # Add the exporter to the tracer provider
            tracer_provider.add_span_processor(BatchSpanProcessor(tempo_exporter))

        except Exception as e:
            logger.warning(f"Failed to configure Tempo exporter: {e}")
            logger.warning(
                "Continuing without Tempo tracing. Make sure Tempo is running at the configured URL.",
            )
            # Continue without Tempo tracing

    # Add file-based exporter for local development
    # Prefer otel_log_dir if set (allows sharing with Unity), otherwise fall back to log_dir
    otel_log_dir = settings.otel_log_dir or settings.log_dir
    if settings.log_enabled and otel_log_dir:
        try:
            from orchestra.web.api.utils.file_trace_exporter import FileSpanExporter

            file_exporter = FileSpanExporter(otel_log_dir)
            tracer_provider.add_span_processor(BatchSpanProcessor(file_exporter))
            logger.info(
                f"Configured file-based trace exporter at {otel_log_dir}",
            )
        except Exception as e:
            logger.warning(f"Failed to configure file trace exporter: {e}")

    excluded_endpoints = [
        app.url_path_for("health_check"),
        app.url_path_for("openapi"),
        app.url_path_for("swagger_ui_html"),
        app.url_path_for("swagger_ui_redirect"),
        app.url_path_for("redoc_html"),
        app.url_path_for("metrics"),
    ]

    FastAPIInstrumentor().instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls=",".join(excluded_endpoints),
    )
    SQLAlchemyInstrumentor().instrument(
        tracer_provider=tracer_provider,
        engine=app.state.db_engine,
    )
    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
    logger.info("Instrumented OpenAI client for tracing")

    # Instrument httpx to capture actual HTTP request timing for OpenAI SDK calls
    # This provides visibility into individual HTTP requests, retries, and rate limiting
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    logger.info("Instrumented httpx client for HTTP-level tracing")

    set_tracer_provider(tracer_provider=tracer_provider)


def stop_opentelemetry(app: FastAPI) -> None:  # pragma: no cover
    """
    Disables opentelemetry instrumentation.

    :param app: current application.
    """
    if not settings.otel_enabled:
        return

    FastAPIInstrumentor().uninstrument_app(app)
    SQLAlchemyInstrumentor().uninstrument()
    OpenAIInstrumentor().uninstrument()
    HTTPXClientInstrumentor().uninstrument()


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
        ORGANIZATION_NAME = settings.orchestra_organization_name
        OWNER_ID = settings.orchestra_owner_id
        PROJ_NAME = settings.orchestra_prod_traffic_name
        logging.info(
            f"Ensuring {ORGANIZATION_NAME} with owner {OWNER_ID} and {PROJ_NAME} exist",
        )
        orgs = org_dao.filter(name=ORGANIZATION_NAME)
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
            project=settings.vertexai_project,
            location=settings.vertexai_location,
        )
        app.middleware_stack = app.build_middleware_stack()
        # ensure_production_traffic_project_exists(app)
        pass  # noqa: WPS420

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
        app.state.db_engine.dispose()

        stop_opentelemetry(app)
        pass  # noqa: WPS420

    return _shutdown
