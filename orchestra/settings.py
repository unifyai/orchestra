"""Platform settings — extends orchestra-core's kernel settings."""

import os
from typing import Optional

from pydantic_settings import SettingsConfigDict

from orchestra_core.settings import LogLevel, Settings as CoreSettings, UniqueValidationMode

__all__ = ["LogLevel", "Settings", "UniqueValidationMode", "settings"]


class Settings(CoreSettings):
    """Platform application settings.

    Adds multi-tenant, billing, voice provider, GCP, OAuth, and Stripe
    configuration on top of the kernel settings.
    """

    is_staging: bool = os.environ.get("STAGING", "False") == "True"

    use_cloud_sql: bool = (
        os.environ.get("ORCHESTRA_USE_CLOUD_SQL", "false").lower() == "true"
    )
    cloud_sql_instance: str = os.environ.get(
        "ORCHESTRA_CLOUD_SQL_INSTANCE",
        "gcp-project-saas:europe-west1:dev",
    )

    sentry_dsn: Optional[str] = None
    sentry_sample_rate: float = 1.0

    orchestra_organization_name: str = os.environ.get(
        "ORCHESTRA_ORGANIZATION_NAME",
        "Unify",
    )
    orchestra_owner_id: str = os.environ.get(
        "ORCHESTRA_OWNER_ID",
        "67abcd12-1fac-4a8f-afe9-c54698c96971",
    )
    chat_completions_project_name: str = "Usage"
    chat_completions_markup_rate: float = 1.2

    console_url: str = os.environ.get(
        "UNIFY_CONSOLE_FRONTEND_URL",
        "https://console.unify.ai/",
    ).rstrip("/")

    gcp_project: str = os.environ.get("GCP_PROJECT_ID", "gcp-project-saas")
    gcp_location: str = os.environ.get("GCP_LOCATION", "europe-west1")

    google_service_sender_email: Optional[str] = os.environ.get("ONBOARDING_EMAIL")
    google_service_account_key_path: Optional[str] = os.environ.get(
        "MAIL_SENDER_SERVICE_ACCOUNT_KEY",
        "/secrets/gcp/mail_sender_service_account_key.json",
    )

    google_oauth_client_id: Optional[str] = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    microsoft_byod_client_id: Optional[str] = os.environ.get("MICROSOFT_BYOD_CLIENT_ID")

    oauth_state_signing_key: Optional[str] = os.environ.get("OAUTH_STATE_SIGNING_KEY")

    selected_voice_provider: Optional[str] = "elevenlabs"
    cartesia_api_key: Optional[str] = os.environ.get("CARTESIA_API_KEY")
    cartesia_api_version: Optional[str] = os.environ.get("CARTESIA_API_VERSION")
    elevenlabs_api_key: Optional[str] = os.environ.get("ELEVENLABS_API_KEY")
    deepgram_api_key: Optional[str] = os.environ.get("DEEPGRAM_API_KEY")
    openai_api_key: Optional[str] = None

    turnstile_secret_key: Optional[str] = os.environ.get("TURNSTILE_SECRET_KEY")

    email_verify_token_secret: Optional[str] = os.environ.get(
        "EMAIL_VERIFY_TOKEN_SECRET",
    )
    mfa_encryption_key: Optional[str] = os.environ.get("MFA_ENCRYPTION_KEY")
    mfa_kms_keyring: str = os.environ.get("MFA_KMS_KEYRING", "mfa")
    mfa_kms_key: str = os.environ.get("MFA_KMS_KEY", "mfa-secrets")

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
    stripe_default_credit_qty: int = int(
        os.environ.get("STRIPE_DEFAULT_CREDIT_QTY", "25"),
    )
    stripe_min_credit_qty: int = int(
        os.environ.get("STRIPE_MIN_CREDIT_QTY", "5"),
    )
    stripe_max_credit_qty: int = int(
        os.environ.get("STRIPE_MAX_CREDIT_QTY", "500"),
    )

    max_promo_amount: float = 100.0

    signup_credit_grant: float = float(
        os.environ.get("SIGNUP_CREDIT_GRANT", "50"),
    )

    assistant_creation_cost: float = 10.0

    photo_generation_cost: float = 0.05
    video_generation_cost: float = 0.20
    default_video_duration: int = 5
    replicate_api_key: Optional[str] = None

    inactivity_followup_days: int = 3
    inactivity_auto_cleanup_days: int = 7
    inactivity_followup_batch_size: int = 200
    inactivity_followup_jitter_seconds: int = 600

    @property
    def db_url(self):
        """Assemble database URL, routing through Cloud SQL Auth Proxy when present."""
        socket_dir = f"/cloudsql/{self.cloud_sql_instance}"
        if os.path.isdir(socket_dir):
            from urllib.parse import quote

            from yarl import URL

            return URL(
                f"postgresql+psycopg2://"
                f"{quote(self.db_user, safe='')}:"
                f"{quote(self.db_pass, safe='')}@"
                f"/{self.db_base}?host={socket_dir}",
            )
        return super().db_url

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ORCHESTRA_",
        env_file_encoding="utf-8",
        extra="allow",
    )


settings = Settings()
