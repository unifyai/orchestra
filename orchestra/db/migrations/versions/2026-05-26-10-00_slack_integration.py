"""Slack integration tables: per-workspace install, channel bindings, thread routes.

Adds three tables that together let an org *or* a personal Unify user run
Slack through a single OAuth-installed app that fans out to many
assistants:

* ``slack_installs`` — one row per ``(owner, slack_team_id)`` where
  ``owner`` is either a Unify organization or a Unify user (exactly one
  is set, enforced by ``ck_slack_install_one_owner``). Holds the bot
  user id, bot access token, and granted scopes. At most one *active*
  (non-revoked) row may exist per Slack workspace.

* ``slack_channel_bindings`` — pins a Slack channel to one default
  assistant in the install's owner scope. Used to resolve un-tokened
  mentions in channels the assistant has been invited to.

* ``slack_thread_routes`` — sticky routing for a single conversation.
  ``thread_ts`` is either the Slack thread root timestamp (channel
  threads) or the sentinel ``__dm_root__`` (DM roots). Rows are TTL'd
  via ``expires_at`` (default 14 days, refreshed on use).

The kernel models in orchestra-core are untouched. Constraint names match
what ``sa.ForeignKey`` / ``UniqueConstraint(name=...)`` / partial-unique
``Index(..., postgresql_where=...)`` produce via ``meta.create_all`` so
fresh test DBs and migrated production DBs agree.

Revision ID: 2026_slack_integration
Revises: 2026_platform_drift_fixes
Create Date: 2026-05-26 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2026_slack_integration"
down_revision = "2026_platform_drift_fixes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slack_installs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("slack_team_id", sa.String(), nullable=False),
        sa.Column("slack_team_name", sa.String(), nullable=True),
        sa.Column("slack_app_id", sa.String(), nullable=False),
        sa.Column("enterprise_id", sa.String(), nullable=True),
        sa.Column("bot_user_id", sa.String(), nullable=False),
        sa.Column("bot_access_token", sa.Text(), nullable=False),
        sa.Column("installer_user_id", sa.String(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column(
            "installed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(organization_id IS NULL) <> (user_id IS NULL)",
            name="ck_slack_install_one_owner",
        ),
    )
    op.create_index(
        "ix_slack_installs_organization_id",
        "slack_installs",
        ["organization_id"],
    )
    op.create_index(
        "ix_slack_installs_user_id",
        "slack_installs",
        ["user_id"],
    )
    op.create_index(
        "ix_slack_installs_team_id",
        "slack_installs",
        ["slack_team_id"],
    )
    # One row per (org, workspace) for org installs.
    op.create_index(
        "ux_slack_install_org_team",
        "slack_installs",
        ["organization_id", "slack_team_id"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    # One row per (user, workspace) for personal installs.
    op.create_index(
        "ux_slack_install_user_team",
        "slack_installs",
        ["user_id", "slack_team_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    # A Slack workspace can carry at most one live bot identity at a time.
    # Revoked rows are kept (audit) but free up the workspace for a
    # different owner to install fresh.
    op.create_index(
        "ux_slack_install_active_team",
        "slack_installs",
        ["slack_team_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "slack_channel_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "install_id",
            sa.Integer(),
            sa.ForeignKey("slack_installs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("channel_name", sa.String(), nullable=True),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bound_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "install_id",
            "channel_id",
            name="uq_slack_channel_binding",
        ),
    )
    op.create_index(
        "ix_slack_channel_bindings_install_id",
        "slack_channel_bindings",
        ["install_id"],
    )
    op.create_index(
        "ix_slack_channel_bindings_assistant_id",
        "slack_channel_bindings",
        ["assistant_id"],
    )

    op.create_table(
        "slack_thread_routes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "install_id",
            sa.Integer(),
            sa.ForeignKey("slack_installs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("thread_ts", sa.String(), nullable=False),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_used_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "install_id",
            "channel_id",
            "thread_ts",
            name="uq_slack_thread_route",
        ),
    )
    op.create_index(
        "ix_slack_thread_routes_install_id",
        "slack_thread_routes",
        ["install_id"],
    )
    op.create_index(
        "ix_slack_thread_routes_assistant_id",
        "slack_thread_routes",
        ["assistant_id"],
    )
    op.create_index(
        "ix_slack_thread_routes_expires",
        "slack_thread_routes",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_slack_thread_routes_expires",
        table_name="slack_thread_routes",
    )
    op.drop_index(
        "ix_slack_thread_routes_assistant_id",
        table_name="slack_thread_routes",
    )
    op.drop_index(
        "ix_slack_thread_routes_install_id",
        table_name="slack_thread_routes",
    )
    op.drop_table("slack_thread_routes")

    op.drop_index(
        "ix_slack_channel_bindings_assistant_id",
        table_name="slack_channel_bindings",
    )
    op.drop_index(
        "ix_slack_channel_bindings_install_id",
        table_name="slack_channel_bindings",
    )
    op.drop_table("slack_channel_bindings")

    op.drop_index(
        "ux_slack_install_active_team",
        table_name="slack_installs",
    )
    op.drop_index(
        "ux_slack_install_user_team",
        table_name="slack_installs",
    )
    op.drop_index(
        "ux_slack_install_org_team",
        table_name="slack_installs",
    )
    op.drop_index(
        "ix_slack_installs_team_id",
        table_name="slack_installs",
    )
    op.drop_index(
        "ix_slack_installs_user_id",
        table_name="slack_installs",
    )
    op.drop_index(
        "ix_slack_installs_organization_id",
        table_name="slack_installs",
    )
    op.drop_table("slack_installs")
