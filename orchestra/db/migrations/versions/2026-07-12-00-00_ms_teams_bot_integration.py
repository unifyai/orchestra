"""MS Teams bot tables: per-tenant install, channel bindings, conversation routes.

Adds the Bot-Framework analogue of the Slack integration: a single Azure
bot registration that a company installs from the Teams Store, after
which inbound activities for that Microsoft tenant fan out to the
assistants of one Unify owner.

* ``ms_teams_bot_installs`` — one row per ``(owner, tenant_id)`` where the
  owner is either a Unify organization or a Unify user. Unlike Slack, a
  Teams Store install arrives before we know the Unify owner, so a row is
  created *pending* (both owners NULL, ``bind_nonce`` set) and later bound
  via the tenant-to-org handshake. ``ck_ms_teams_bot_install_single_owner``
  forbids *both* owners at once while permitting the pending state. At most
  one *active* (non-revoked) row may exist per Microsoft tenant. Tokens are
  minted on demand from the shared ``bot_app_id`` + secret, so we persist
  the tenant's ``service_url`` for outbound proactive replies instead of a
  bot token.

* ``ms_teams_bot_channel_bindings`` — pins a Teams channel to one default
  assistant in the install's owner scope.

* ``ms_teams_bot_conversation_routes`` — sticky routing keyed on the Bot
  Framework ``conversation.id`` (uniquely identifies a 1:1 chat, group
  chat, or channel thread). ``conversation_reference`` stores the
  serialized ConversationReference so outbound can reply proactively. Rows
  are TTL'd via ``expires_at`` (default 14 days, refreshed on use).

Constraint / index names match what ``meta.create_all`` produces from the
ORM models so fresh test DBs and migrated production DBs agree.

Revision ID: 2026_ms_teams_bot_integration
Revises: user_voice_sample
Create Date: 2026-07-12 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2026_ms_teams_bot_integration"
down_revision = "user_voice_sample"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ms_teams_bot_installs",
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
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("tenant_name", sa.String(), nullable=True),
        sa.Column("bot_app_id", sa.String(), nullable=False),
        sa.Column("service_url", sa.String(), nullable=True),
        sa.Column("installer_aad_object_id", sa.String(), nullable=True),
        sa.Column("bind_nonce", sa.String(), nullable=True),
        sa.Column("bound_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            "NOT (organization_id IS NOT NULL AND user_id IS NOT NULL)",
            name="ck_ms_teams_bot_install_single_owner",
        ),
    )
    op.create_index(
        "ix_ms_teams_bot_installs_organization_id",
        "ms_teams_bot_installs",
        ["organization_id"],
    )
    op.create_index(
        "ix_ms_teams_bot_installs_user_id",
        "ms_teams_bot_installs",
        ["user_id"],
    )
    op.create_index(
        "ix_ms_teams_bot_installs_tenant_id",
        "ms_teams_bot_installs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_ms_teams_bot_installs_bind_nonce",
        "ms_teams_bot_installs",
        ["bind_nonce"],
    )
    # One row per (org, tenant) for org installs.
    op.create_index(
        "ux_ms_teams_bot_install_org_tenant",
        "ms_teams_bot_installs",
        ["organization_id", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    # One row per (user, tenant) for personal installs.
    op.create_index(
        "ux_ms_teams_bot_install_user_tenant",
        "ms_teams_bot_installs",
        ["user_id", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    # A Microsoft tenant can carry at most one live bot install at a time.
    # Revoked rows are kept (audit) but free up the tenant for a fresh
    # install.
    op.create_index(
        "ux_ms_teams_bot_install_active_tenant",
        "ms_teams_bot_installs",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "ms_teams_bot_channel_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "install_id",
            sa.Integer(),
            sa.ForeignKey("ms_teams_bot_installs.id", ondelete="CASCADE"),
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
            name="uq_ms_teams_bot_channel_binding",
        ),
    )
    op.create_index(
        "ix_ms_teams_bot_channel_bindings_install_id",
        "ms_teams_bot_channel_bindings",
        ["install_id"],
    )
    op.create_index(
        "ix_ms_teams_bot_channel_bindings_assistant_id",
        "ms_teams_bot_channel_bindings",
        ["assistant_id"],
    )

    op.create_table(
        "ms_teams_bot_conversation_routes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "install_id",
            sa.Integer(),
            sa.ForeignKey("ms_teams_bot_installs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_reference", sa.Text(), nullable=True),
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
            "conversation_id",
            name="uq_ms_teams_bot_conversation_route",
        ),
    )
    op.create_index(
        "ix_ms_teams_bot_conversation_routes_install_id",
        "ms_teams_bot_conversation_routes",
        ["install_id"],
    )
    op.create_index(
        "ix_ms_teams_bot_conversation_routes_assistant_id",
        "ms_teams_bot_conversation_routes",
        ["assistant_id"],
    )
    op.create_index(
        "ix_ms_teams_bot_conversation_routes_expires",
        "ms_teams_bot_conversation_routes",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ms_teams_bot_conversation_routes_expires",
        table_name="ms_teams_bot_conversation_routes",
    )
    op.drop_index(
        "ix_ms_teams_bot_conversation_routes_assistant_id",
        table_name="ms_teams_bot_conversation_routes",
    )
    op.drop_index(
        "ix_ms_teams_bot_conversation_routes_install_id",
        table_name="ms_teams_bot_conversation_routes",
    )
    op.drop_table("ms_teams_bot_conversation_routes")

    op.drop_index(
        "ix_ms_teams_bot_channel_bindings_assistant_id",
        table_name="ms_teams_bot_channel_bindings",
    )
    op.drop_index(
        "ix_ms_teams_bot_channel_bindings_install_id",
        table_name="ms_teams_bot_channel_bindings",
    )
    op.drop_table("ms_teams_bot_channel_bindings")

    op.drop_index(
        "ux_ms_teams_bot_install_active_tenant",
        table_name="ms_teams_bot_installs",
    )
    op.drop_index(
        "ux_ms_teams_bot_install_user_tenant",
        table_name="ms_teams_bot_installs",
    )
    op.drop_index(
        "ux_ms_teams_bot_install_org_tenant",
        table_name="ms_teams_bot_installs",
    )
    op.drop_index(
        "ix_ms_teams_bot_installs_bind_nonce",
        table_name="ms_teams_bot_installs",
    )
    op.drop_index(
        "ix_ms_teams_bot_installs_tenant_id",
        table_name="ms_teams_bot_installs",
    )
    op.drop_index(
        "ix_ms_teams_bot_installs_user_id",
        table_name="ms_teams_bot_installs",
    )
    op.drop_index(
        "ix_ms_teams_bot_installs_organization_id",
        table_name="ms_teams_bot_installs",
    )
    op.drop_table("ms_teams_bot_installs")
