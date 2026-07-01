import enum
import os
from pathlib import Path
from tempfile import gettempdir
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from yarl import URL

from orchestra.env import get_env
from orchestra.lib.deploy_env import resolve_deploy_env

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

    @property
    def is_staging(self) -> bool:
        return resolve_deploy_env() == "staging"

    @property
    def is_self_host(self) -> bool:
        return os.environ.get("SELF_HOST", "0") == "1"

    @property
    def manual_topup(self) -> bool:
        """Whether this deployment meters credits but tops up for free.

        Staging exercises the full billing path (credits deplete with usage
        and gate further work) without Stripe: developers replenish credits
        with a free self-serve top-up so runaway spend is impossible. An
        explicit ``MANUAL_TOPUP`` override makes the mode reproducible in
        local/CI stacks where ``DEPLOY_ENV`` is not ``staging``.
        """
        override = os.environ.get("MANUAL_TOPUP")
        if override is not None:
            return override == "1"
        return self.is_staging

    @property
    def account_reset(self) -> bool:
        """Whether the staging-only "reset my account" tool is available.

        The tool rewinds a user's personal workspace to its fresh-signup state,
        so it is confined to staging. An explicit ``ACCOUNT_RESET`` override
        enables it in local/CI stacks where ``DEPLOY_ENV`` is not ``staging`` so
        the flow is reproducible and E2E-testable.
        """
        override = os.environ.get("ACCOUNT_RESET")
        if override is not None:
            return override == "1"
        return self.is_staging

    @property
    def charges_billing(self) -> bool:
        """Whether credit pre-checks and deductions run for billable actions.

        Enabled in production and in manual-top-up mode (staging); disabled on
        self-host, which has no payment processor at all.
        """
        return self.manual_topup or (not self.is_staging and not self.is_self_host)

    @property
    def billing_enabled(self) -> bool:
        """Whether billing/checkout is operational on this deployment.

        Orchestra owns the authoritative billing credentials (Stripe secret +
        subscription prices), so it is the source of truth for whether the
        billing experience should be surfaced at all. Consumers (e.g. Console)
        read this via ``GET /v0/features`` instead of guessing from their own
        partial env. Distinct from ``charges_billing``, which governs credit
        metering rather than whether the billing UI can transact.
        """
        return (
            bool(self.stripe_secret_key)
            and bool(self.stripe_unify_subscription_price_id_personal_monthly)
            and bool(self.stripe_unify_subscription_price_id_personal_annual)
            and bool(self.stripe_unify_subscription_price_id_business_monthly)
            and bool(self.stripe_unify_subscription_price_id_business_annual)
            and bool(self.stripe_unify_annual_coupon_id)
        )

    @property
    def workspace_google_enabled(self) -> bool:
        """Whether assistants can connect a Google workspace (BYOD OAuth).

        Orchestra holds the Google OAuth client ID used to build the
        authorization URL, so it is the source of truth for whether the
        connect-Google-workspace flow can run. Consumers read this via
        ``GET /v0/features`` rather than checking their own env.
        """
        return bool(self.google_oauth_client_id)

    @property
    def workspace_microsoft_enabled(self) -> bool:
        """Whether assistants can connect a Microsoft workspace (BYOD OAuth).

        Mirrors ``workspace_google_enabled`` for the Microsoft 365 client ID.
        """
        return bool(self.microsoft_byod_client_id)

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
        "gcp-project-saas:europe-west1:dev",  # Format: "project:region:instance"
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

    # Unify admin organization (used for demo-assistant gating and other
    # admin-only features). The org row itself is provisioned out of band;
    # these settings only record its name and owner id for lookup.
    orchestra_organization_name: str = os.environ.get(
        "ORCHESTRA_ORGANIZATION_NAME",
        "Unify",
    )
    orchestra_owner_id: str = os.environ.get(
        "ORCHESTRA_OWNER_ID",
        "67abcd12-1fac-4a8f-afe9-c54698c96971",
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

    gcp_project: str = os.environ.get("GCP_PROJECT_ID", "gcp-project-saas")
    gcp_location: str = os.environ.get("GCP_LOCATION", "europe-west1")

    # Variables for email sending
    google_service_sender_email: Optional[str] = os.environ.get("ONBOARDING_EMAIL")
    google_service_account_key_path: Optional[str] = os.environ.get(
        "MAIL_SENDER_SERVICE_ACCOUNT_KEY",
        "/secrets/gcp/mail_sender_service_account_key.json",
    )

    # BYOD email OAuth client IDs (for building OAuth authorization URLs)
    google_oauth_client_id: Optional[str] = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    microsoft_byod_client_id: Optional[str] = os.environ.get(
        "MICROSOFT_BYOD_CLIENT_ID",
    )

    # HMAC-SHA256 key for signing OAuth state params (shared with Communication)
    oauth_state_signing_key: Optional[str] = os.environ.get("OAUTH_STATE_SIGNING_KEY")

    # Variables for voice management
    selected_voice_provider: Optional[str] = "elevenlabs"
    cartesia_api_key: Optional[str] = os.environ.get("CARTESIA_API_KEY")
    cartesia_api_version: Optional[str] = os.environ.get("CARTESIA_API_VERSION")
    elevenlabs_api_key: Optional[str] = os.environ.get("ELEVENLABS_API_KEY")
    deepgram_api_key: Optional[str] = os.environ.get("DEEPGRAM_API_KEY")
    openrouter_api_key: Optional[str] = get_env("ORCHESTRA_OPENROUTER_API_KEY")
    openrouter_api_base: str = get_env(
        "ORCHESTRA_OPENROUTER_API_BASE",
        "https://openrouter.ai/api/v1",
    )

    # Cloudflare Turnstile CAPTCHA
    turnstile_secret_key: Optional[str] = os.environ.get("TURNSTILE_SECRET_KEY")

    # Email-auth & MFA secrets
    email_verify_token_secret: Optional[str] = os.environ.get(
        "EMAIL_VERIFY_TOKEN_SECRET",
    )
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
    # Recurring prices backing the self-serve subscription plans. Created by
    # ``scripts/create_subscription_prices.py`` under the existing Unify
    # Credits products. At 1 credit = $1 these are per-unit ($1/credit/month)
    # prices; the tier is the subscription quantity.
    stripe_unify_subscription_price_id_personal_monthly: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_PERSONAL_MONTHLY",
    )
    stripe_unify_subscription_price_id_business_monthly: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_BUSINESS_MONTHLY",
    )
    # Annual variants of the recurring subscription prices (interval=year,
    # $12/credit/year so the annual list price == 12× the monthly tier).
    # The annual *discount* is expressed as a Stripe coupon applied at
    # subscribe time (price-only — the credit grant stays 12× the monthly
    # tier), so the discount can be tuned without re-minting prices.
    stripe_unify_subscription_price_id_personal_annual: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_PERSONAL_ANNUAL",
    )
    stripe_unify_subscription_price_id_business_annual: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_BUSINESS_ANNUAL",
    )
    #: Stripe coupon id applied to annual subscriptions to express the
    #: annual discount (e.g. "two months free"). Optional — when unset the
    #: annual price is charged at its full list rate.
    stripe_unify_annual_coupon_id: Optional[str] = os.environ.get(
        "STRIPE_UNIFY_ANNUAL_COUPON_ID",
    )
    # NOTE: STRIPE_DEFAULT/MIN/MAX_CREDIT_QTY were retired with the one-time
    # credit Checkout (``POST /v0/billing/checkout-session``) under the
    # self-serve subscription model. Self-serve accounts now receive credits
    # via their monthly subscription plan grant; there is no adjustable
    # one-time top-up quantity any more.

    # Promo credits
    max_promo_amount: float = 100.0

    # Signup credit grant (free credits for new users)
    signup_credit_grant: float = float(
        os.environ.get("SIGNUP_CREDIT_GRANT", "100"),
    )

    # ── Referral program ────────────────────────────────────────────────
    #: Master switch. When False, referral codes can still exist but no
    #: attribution or reward is processed.
    referral_enabled: bool = os.environ.get(
        "REFERRAL_ENABLED",
        "true",
    ).lower() not in ("0", "false", "no")

    #: Flat referrer reward (USD value; shown ×``display_credits_per_usd`` in
    #: customer surfaces, so $100 → "40,000 credits"). Paid in free
    #: (promotional) credits once the referred friend has subscribed and
    #: spent ``referral_qualifying_spend`` of real money on the platform.
    referral_reward_credits: float = float(
        os.environ.get("REFERRAL_REWARD_CREDITS", "100"),
    )

    #: Flat bonus (USD-denominated credits) granted to the *referred* friend
    #: once they qualify the reward. Two-sided incentive; spend-gated.
    referral_referee_bonus_credits: float = float(
        os.environ.get("REFERRAL_REFEREE_BONUS_CREDITS", "50"),
    )

    #: Referral reward/bonus credits expire this many days after they are
    #: granted (unconsumed remainder is forfeited by the credit-grant sweep).
    referral_reward_expiry_days: int = int(
        os.environ.get("REFERRAL_REWARD_EXPIRY_DAYS", "90"),
    )

    #: Cumulative real-money spend (USD) the referred friend must reach
    #: *after* subscribing before the reward unlocks. Rewarding on realised
    #: spend (not signup or a single invoice) keeps fake/low-value signups
    #: from farming rewards.
    referral_qualifying_spend: float = float(
        os.environ.get("REFERRAL_QUALIFYING_SPEND", "100"),
    )

    #: Anti-abuse cap on how many referrals a single referrer can be
    #: *rewarded* for, in total. ``0`` disables the cap.
    referral_max_rewarded_per_referrer: int = int(
        os.environ.get("REFERRAL_MAX_REWARDED_PER_REFERRER", "100"),
    )

    #: Display-only credit framing. The wallet/ledger denominate in
    #: canonical USD value (1 internal unit = $1); customer-facing surfaces
    #: (console + outbound emails) render *credits* = USD × this multiplier.
    #: It is purely cosmetic — never used for settlement or anything sent to
    #: Stripe. Keep in sync with the console ``DISPLAY_CREDITS_PER_USD``.
    display_credits_per_usd: int = int(
        os.environ.get("DISPLAY_CREDITS_PER_USD", "400"),
    )

    #: How many days before an expiring credit grant lapses to email the
    #: account holder a "use-it-or-lose-it" reminder (see
    #: ``orchestra.routines.credit_expiry_reminder``).
    credit_expiry_reminder_days: int = int(
        os.environ.get("CREDIT_EXPIRY_REMINDER_DAYS", "3"),
    )

    # Assistant creation
    assistant_creation_cost: float = 0.0
    unity_coordinator_whatsapp_number: Optional[str] = (
        os.environ.get("UNITY_COORDINATOR_WHATSAPP_NUMBER")
        or os.environ.get("UNITY_WHATSAPP_POOL_NUMBER")
        or os.environ.get("ORCHESTRA_UNITY_WHATSAPP_POOL_NUMBER")
    )
    unity_coordinator_email_address: Optional[str] = (
        os.environ.get("UNITY_COORDINATOR_EMAIL_ADDRESS")
        or os.environ.get("ORCHESTRA_UNITY_COORDINATOR_EMAIL_ADDRESS")
        or "twin@unify.ai"
    )
    # Discrete per-country Coordinator phone numbers. The UK number is keyed
    # under ISO country code "GB". The correct prod/staging value is mounted
    # from Secret Manager per service.
    unity_coordinator_phone_uk: Optional[str] = os.environ.get(
        "UNITY_COORDINATOR_PHONE_UK",
    ) or os.environ.get("ORCHESTRA_UNITY_COORDINATOR_PHONE_UK")
    unity_coordinator_phone_us: Optional[str] = os.environ.get(
        "UNITY_COORDINATOR_PHONE_US",
    ) or os.environ.get("ORCHESTRA_UNITY_COORDINATOR_PHONE_US")
    unity_coordinator_default_phone_country: str = (
        os.environ.get("UNITY_COORDINATOR_DEFAULT_PHONE_COUNTRY")
        or os.environ.get("ORCHESTRA_UNITY_COORDINATOR_DEFAULT_PHONE_COUNTRY")
        or "US"
    )
    # Universal Coordinator Discord bot. The bot ID is a Discord snowflake;
    # the token authenticates the Gateway connection. Unity pulls both from
    # Orchestra's shared pool, so the secrets only live in this project.
    unity_coordinator_discord_id: Optional[str] = os.environ.get(
        "UNITY_COORDINATOR_DISCORD_ID",
    ) or os.environ.get("ORCHESTRA_UNITY_COORDINATOR_DISCORD_ID")
    unity_coordinator_discord_token: Optional[str] = os.environ.get(
        "UNITY_COORDINATOR_DISCORD_TOKEN",
    ) or os.environ.get("ORCHESTRA_UNITY_COORDINATOR_DISCORD_TOKEN")

    # Assistant photo generation
    photo_generation_cost: float = (
        0.05  # /img. See https://replicate.com/black-forest-labs/flux-1.1-pro
    )
    video_generation_cost: float = (
        0.20  # /s. See https://replicate.com/bytedance/omni-human-1.5
    )
    default_video_duration: int = (
        5  # Fallback when client does not send duration (billing only)
    )
    replicate_api_key: Optional[str] = None  # Populated by model_config below

    # Re-engagement follow-up routine.
    # inactivity_followup_days: days a user can go without interacting
    #   with ANY of their assistants (incl. the Coordinator) before their
    #   personal Coordinator is woken to send a re-engagement follow-up.
    #   Measured per-user from the most recent correspondence across all
    #   their assistants (or from signup for users who never engaged).
    #   Orchestra only decides who/when; the Coordinator's brain composes
    #   and sends the actual re-engagement message.
    # inactivity_followup_batch_size: upper bound on users processed per
    #   routine invocation — caps blast radius and keeps the daily Cloud
    #   Scheduler run bounded.
    # inactivity_followup_jitter_seconds: per-user random delay
    #   (0..jitter) applied when dispatching follow-ups so a daily run
    #   does not wake hundreds of Coordinators in the same second.
    #
    # Note: this routine no longer deletes or deprovisions inactive
    # assistants — contact lifecycle/cost is governed solely by the
    # billing suspension routine (assistant_contact_suspension). The
    # follow-up is purely a gentle re-engagement prompt, and users who
    # explicitly opt out (``inactivity_followup_opted_out``) are skipped.
    inactivity_followup_days: int = 7
    inactivity_followup_batch_size: int = 200
    inactivity_followup_jitter_seconds: int = 600

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
