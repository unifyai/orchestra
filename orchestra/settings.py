import enum
import os
from pathlib import Path
from tempfile import gettempdir
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from yarl import URL

TEMP_DIR = Path(gettempdir())


class LogLevel(str, enum.Enum):  # noqa: WPS600
    """Possible log levels."""

    NOTSET = "NOTSET"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


class UniqueValidationMode(str, enum.Enum):
    """
    Mode for unique field validation.

    JSONB_SCAN: Original behavior - scan all logs with JSONB containment (slow, O(N×M))
    LOOKUP_TABLE: New behavior - use lookup table with B-tree index (fast, O(M×log N))

    Controlled by ORCHESTRA_UNIQUE_VALIDATION_MODE environment variable.
    Default is JSONB_SCAN for backward compatibility during migration.
    """

    JSONB_SCAN = "jsonb_scan"
    LOOKUP_TABLE = "lookup_table"


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
    # HTTP keep-alive timeout in seconds (how long to keep idle connections open)
    timeout_keep_alive: int = 15

    # Inactivity timeout in seconds for local development
    # When set, the server will shut down after this many seconds without API requests
    # Default (None) means no timeout - server runs indefinitely
    inactivity_timeout_seconds: Optional[int] = None

    # Current environment
    environment: str = "dev"
    is_staging: bool = os.environ.get("STAGING", "False") == "True"

    log_level: LogLevel = LogLevel.INFO
    # Variables for the database
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = os.environ.get("ORCHESTRA_DB_USER", "")
    db_pass: str = os.environ.get("ORCHESTRA_DB_PASS", "")
    db_base: str = os.environ.get("ORCHESTRA_DB_BASE", "")
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

    # OpenTelemetry master switch
    # Set to "false" to disable all OTel tracing
    otel_enabled: bool = os.environ.get("ORCHESTRA_OTEL", "true").lower() in (
        "true",
        "1",
    )

    # OTLP endpoint for OpenTelemetry export (e.g., http://localhost:4317)
    # When set, traces are exported via OTLP to Tempo/Jaeger
    otel_endpoint: Optional[str] = os.environ.get("ORCHESTRA_OTEL_ENDPOINT")

    # Use secure (TLS) connection for OTLP export
    otel_secure: bool = os.environ.get("ORCHESTRA_OTEL_SECURE", "").lower() == "true"

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

    # Master logging switch (console + file if log_dir is set)
    # Set to "false" to disable all logging
    log_enabled: bool = os.environ.get("ORCHESTRA_LOG", "true").lower() in ("true", "1")

    # Local file-based logging directory
    # When set, traces are written to JSON files in this directory
    # Example: /Users/user/unity/logs/orchestra/2025-01-01T12-00-00
    log_dir: Optional[str] = os.environ.get(
        "ORCHESTRA_LOG_DIR",
        None,
    )

    # OTel span log directory (for file-based span export)
    # When set, OTel spans are written to JSONL files in this directory.
    # If not set, falls back to log_dir for backward compatibility.
    # This enables writing spans to a shared directory with Unity for
    # full-stack trace correlation across processes.
    # Example: /Users/user/unity/logs/otel
    otel_log_dir: Optional[str] = os.environ.get(
        "ORCHESTRA_OTEL_LOG_DIR",
        None,
    )

    # Comma-separated span name patterns to exclude from OTel export.
    # Matched as substrings against span names. Default excludes repetitive
    # auth/connection overhead that adds noise without diagnostic value.
    # Set to empty string (ORCHESTRA_OTEL_EXCLUDE_PATTERNS="") to disable.
    otel_exclude_patterns: list[str] = [
        p.strip()
        for p in os.environ.get(
            "ORCHESTRA_OTEL_EXCLUDE_PATTERNS",
            "connect,db.query.select.users,db.query.select.api_key,"
            "db.query.select.team_member,db.query.select.resource_access",
        ).split(",")
        if p.strip()
    ]

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

    # Console URL for generating shareable plot links
    console_url: str = os.environ.get(
        "UNIFY_CONSOLE_FRONTEND_URL",
        "https://console.unify.ai/",
    ).rstrip("/")

    gcp_project: str = (
        os.environ.get("GCP_PROJECT_ID") if os.environ.get("ON_PREM") else "saas-368716"
    )
    gcp_location: str = (
        os.environ.get("GCP_LOCATION") if os.environ.get("ON_PREM") else "europe-west1"
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

    # Cloudflare Turnstile CAPTCHA
    turnstile_secret_key: Optional[str] = os.environ.get("TURNSTILE_SECRET_KEY")

    # Email-auth & MFA secrets
    mfa_encryption_key: Optional[str] = os.environ.get("MFA_ENCRYPTION_KEY")
    mfa_kms_keyring: str = os.environ.get("MFA_KMS_KEYRING", "mfa")
    mfa_kms_key: str = os.environ.get("MFA_KMS_KEY", "mfa-secrets")

    # Stripe configuration
    stripe_secret_key: Optional[str] = os.environ.get("STRIPE_SECRET_KEY")
    stripe_webhook_secret: Optional[str] = os.environ.get("STRIPE_WEBHOOK_SECRET")
    stripe_skip_signature_verification: bool = (
        os.environ.get("SKIP_STRIPE_SIGNATURE_VERIFICATION", "false").lower() == "true"
    )
    stripe_unify_credits_product_id_personal: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_CREDITS_PRODUCT_ID_PERSONAL",
    )
    stripe_unify_credits_product_id_business: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_CREDITS_PRODUCT_ID_BUSINESS",
    )
    stripe_unify_credits_price_id_personal: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_CREDITS_PRICE_ID_PERSONAL",
    )
    stripe_unify_credits_price_id_business: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_CREDITS_PRICE_ID_BUSINESS",
    )

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
        # When the Cloud SQL Auth Proxy socket exists, route through it
        # so the proxy handles SSL/mTLS automatically.
        socket_dir = f"/cloudsql/{self.cloud_sql_instance}"
        if os.path.isdir(socket_dir):
            from urllib.parse import quote

            return URL(
                f"postgresql+psycopg2://"
                f"{quote(self.db_user, safe='')}:"
                f"{quote(self.db_pass, safe='')}@"
                f"/{self.db_base}?host={socket_dir}",
            )

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

    @property
    def unique_validation_mode(self) -> UniqueValidationMode:
        """
        Get the unique field validation mode.

        Controls how unique field constraints are checked:
        - jsonb_scan: Original O(N×M) JSONB containment scan (slow)
        - lookup_table: New O(M×log N) lookup table approach (fast)

        Default is jsonb_scan for backward compatibility during migration.

        :return: The configured validation mode.
        """
        mode_str = os.environ.get(
            "ORCHESTRA_UNIQUE_VALIDATION_MODE",
            UniqueValidationMode.LOOKUP_TABLE.value,
        )
        try:
            return UniqueValidationMode(mode_str)
        except ValueError:
            return UniqueValidationMode.LOOKUP_TABLE

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ORCHESTRA_",
        env_file_encoding="utf-8",
        extra="allow",
    )


settings = Settings()
