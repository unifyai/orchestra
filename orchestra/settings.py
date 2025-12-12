import enum
import os
from pathlib import Path
from tempfile import gettempdir
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from yarl import URL

TEMP_DIR = Path(gettempdir())

# Runtime storage mode override for testing
_use_jsonb_override: Optional[bool] = None


def set_jsonb_mode(enabled: Optional[bool]) -> None:
    """
    Override storage mode at runtime.

    Args:
        enabled: Storage mode flag (None uses environment variable).
    """
    global _use_jsonb_override
    _use_jsonb_override = enabled


class LogLevel(str, enum.Enum):  # noqa: WPS600
    """Possible log levels."""

    NOTSET = "NOTSET"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


class Settings(BaseSettings):
    """
    Application settings.

    These parameters can be configured
    with environment variables.
    """

    host: str = "127.0.0.1"
    port: int = 8000
    # quantity of workers for uvicorn
    workers_count: int = 1
    # Enable uvicorn reloading
    reload: bool = False

    # Current environment
    environment: str = "dev"
    is_staging: bool = os.environ.get("STAGING", "False") == "True"

    log_level: LogLevel = LogLevel.INFO
    # Variables for the database
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = os.environ.get("ORCHESTRA_DB_USER", "orchestra")
    db_pass: str = os.environ.get("ORCHESTRA_DB_PASS", "orchestra")
    db_base: str = os.environ.get("ORCHESTRA_DB_BASE", "orchestra")
    db_path_query: str = ""
    db_send_host: bool = True
    db_echo: bool = False

    # Cloud SQL configuration
    use_cloud_sql: bool = (
        os.environ.get("ORCHESTRA_USE_CLOUD_SQL", "false").lower()
        == "true"  # Set to True to use Cloud SQL connector instead of direct connection
    )
    cloud_sql_instance: str = os.environ.get(
        "ORCHESTRA_CLOUD_SQL_INSTANCE",
        "saas-368716:europe-west1:dev",  # Format: "project:region:instance"
    )

    # This variable is used to define
    # multiproc_dir. It's required for [uvi|guni]corn projects.
    prometheus_dir: Path = TEMP_DIR / "prom"

    # Sentry's configuration.
    sentry_dsn: Optional[str] = None
    sentry_sample_rate: float = 1.0

    # Grpc endpoint for opentelemetry.
    # E.G. http://localhost:4317
    opentelemetry_endpoint: Optional[str] = os.environ.get(
        "ORCHESTRA_OPENTELEMETRY_ENDPOINT",
    )
    opentelemetry_secure: bool = (
        os.environ.get("ORCHESTRA_OPENTELEMETRY_SECURE", "").lower() == "true"
    )

    # Observability Stack Configuration
    # Set these to None to disable the respective service

    # Loki URL for log aggregation and storage
    # Example: http://localhost:3100
    # Set to None to disable Loki integration
    loki_url: Optional[str] = os.environ.get(
        "ORCHESTRA_LOKI_URL",
        None,
    )
    loki_username: Optional[str] = os.environ.get("ORCHESTRA_LOKI_USERNAME")
    loki_password: Optional[str] = os.environ.get("ORCHESTRA_LOKI_PASSWORD")

    # Tempo URL for distributed tracing backend
    # Example: http://localhost:4317
    # Set to None to disable Tempo integration
    tempo_url: Optional[str] = os.environ.get(
        "ORCHESTRA_TEMPO_URL",
        None,
    )

    # Grafana URL for metrics, logs, and traces visualization
    # Example: http://localhost:3000
    # Set to None to disable Grafana integration
    grafana_url: Optional[str] = os.environ.get(
        "ORCHESTRA_GRAFANA_URL",
        None,
    )

    # Production Traffic Project (for internal monitoring)
    orchestra_organization_name: str = os.environ.get(
        "ORCHESTRA_ORGANIZATION_NAME",
        "Orchestra Admin Organization",
    )
    orchestra_owner_id: str = os.environ.get(
        "ORCHESTRA_OWNER_ID",
        "67abcd12-1fac-4a8f-afe9-c54698c96971",
    )
    orchestra_prod_traffic_name: str = os.environ.get(
        "ORCHESTRA_PROD_TRAFFIC_NAME",
        "Production Traffic",
    )
    traffic_log_pubsub_topic: str = os.environ.get(
        "ORCHESTRA_TRAFFIC_LOG_PUBSUB_TOPIC",
        "orchestra-traffic-logs",
    )
    traffic_log_pubsub_subscription: str = os.environ.get(
        "ORCHESTRA_TRAFFIC_LOG_PUBSUB_SUBSCRIPTION",
        "orchestra-traffic-logs-sub",
    )
    traffic_log_pubsub_project_id: str = os.environ.get(
        "ORCHESTRA_TRAFFIC_LOG_PUBSUB_PROJECT_ID",
        "saas-368716",
    )

    # Chat Completions Project
    chat_completions_project_name: str = "Usage"
    chat_completions_markup_rate: float = 1.2
    cors_allow_origins: list[str] = []

    vertexai_service_acc_json: str = ""
    vertexai_project: str = (
        os.environ.get("ORCHESTRA_VERTEXAI_PROJECT")
        if os.environ.get("ON_PREM")
        else "saas-368716"
    )
    vertexai_location: str = (
        os.environ.get("ORCHESTRA_VERTEXAI_LOCATION")
        if os.environ.get("ON_PREM")
        else "europe-west1"
    )

    # Variables for email sending
    google_service_sender_email: Optional[str] = os.environ.get("ONBOARDING_EMAIL")
    google_service_account_key_path: Optional[str] = os.environ.get(
        "MAIL_SENDER_SERVICE_ACCOUNT_KEY",
        "/secrets/gcp/mail_sender_service_account_key.json",
    )

    # Variables for voice management
    selected_voice_provider: Optional[str] = "elevenlabs"
    cartesia_api_key: Optional[str] = os.environ.get("CARTESIA_API_KEY")
    cartesia_api_version: Optional[str] = os.environ.get("CARTESIA_API_VERSION")
    elevenlabs_api_key: Optional[str] = os.environ.get("ELEVENLABS_API_KEY")
    deepgram_api_key: Optional[str] = os.environ.get("DEEPGRAM_API_KEY")
    openai_api_key: Optional[str] = None  # Populated by model_config below

    # Assistant creation
    assistant_creation_cost: float = 10.0

    # Assistant photo generation
    photo_generation_cost: float = (
        0.05  # /img. See https://replicate.com/black-forest-labs/flux-1.1-pro
    )
    video_generation_cost: float = (
        0.08  # /s. See https://replicate.com/wan-video/wan-2.5-i2v-fast
    )
    default_video_duration: int = (
        5  # Default. See See https://replicate.com/wan-video/wan-2.5-i2v-fast
    )
    replicate_api_key: Optional[str] = None  # Populated by model_config below

    @property
    def db_url(self) -> URL:
        """
        Assemble database URL from settings.

        :return: database URL.
        """
        host = self.db_host
        port = self.db_port
        if not self.db_send_host:
            host = ""
            port = None  # type: ignore

        return URL.build(
            scheme="postgresql+psycopg2",
            host=host,
            port=port,
            user=self.db_user,
            password=self.db_pass,
            path=f"/{self.db_base}",
            query=self.db_path_query,
        )

    @property
    def use_jsonb_queries(self) -> bool:
        """
        Enable JSONB-based query builder.

        Checks runtime override, then environment variable.

        :return: True if JSONB queries are enabled.
        """
        if _use_jsonb_override is not None:
            return _use_jsonb_override
        return os.environ.get("ORCHESTRA_USE_JSONB_QUERIES", "false").lower() == "true"

    @use_jsonb_queries.setter
    def use_jsonb_queries(self, value: bool) -> None:
        """Set JSONB mode override (for testing)."""
        set_jsonb_mode(value)

    @property
    def use_aggregation_cte_optimization(self) -> bool:
        """
        Enable CTE-based aggregation optimization.

        Pre-compute aggregations in CTEs instead of correlated subqueries for improved
        performance on large datasets.

        :return: True if CTE optimization is enabled.
        """
        return (
            os.environ.get(
                "ORCHESTRA_USE_AGGREGATION_CTE_OPTIMIZATION",
                "true",
            ).lower()
            == "true"
        )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ORCHESTRA_",
        env_file_encoding="utf-8",
        extra="allow",
    )


settings = Settings()
