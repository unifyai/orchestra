"""Merge the dashboard-token drop and referral-origin branches.

Both descend from ``drop_legacy_tasks_instance_id``: the dashboards
retirement dropped ``dashboard_token`` on one branch while the deployment
target guard and referral-origin rename landed on the other. Two heads make
``upgrade head`` ambiguous, which is what failed the staging build.

Revision ID: merge_dashboard_referral
Revises: drop_dashboard_token, referral_origin_meaning
"""

from __future__ import annotations

revision = "merge_dashboard_referral"
down_revision = (
    "drop_dashboard_token",
    "referral_origin_meaning",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join both schema branches without changing database objects."""


def downgrade() -> None:
    """Split the migration graph without changing database objects."""
