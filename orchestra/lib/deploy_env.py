"""Canonical deployment-environment resolution.

Every Orchestra deployment is pinned to exactly one environment
("production" or "staging").  All environment-sensitive behavior —
Pub/Sub topic suffixes, staging auth gates, billing metering, bucket
defaults — must resolve through this module so that the signals stay
consistent across the codebase.

``DEPLOY_ENV`` is the single canonical setting; deploy configs pin it
explicitly on every deployment.  When it is unset (e.g. local
development), the environment-scoped ``ORCHESTRA_URL`` decides
(``internal.example.com`` → staging), defaulting to
production.
"""

import os


def resolve_deploy_env() -> str:
    """Return the deployment environment: ``"staging"`` or ``"production"``."""
    deploy_env = (os.environ.get("DEPLOY_ENV") or "").strip().lower()
    if deploy_env == "staging":
        return "staging"
    if deploy_env == "production":
        return "production"
    if "staging" in os.environ.get("ORCHESTRA_URL", "").lower():
        return "staging"
    return "production"


def env_suffix() -> str:
    """Resource-name suffix for the current environment.

    Matches the naming convention used by the comms/adapters services
    (``unity-{assistant_id}-staging`` on staging, unsuffixed in
    production).
    """
    return "-staging" if resolve_deploy_env() == "staging" else ""
