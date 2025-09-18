"""Add voice provider to the Assistants table and use it alongside the voice_id as the Voice table's primary key

Revision ID: 51f08d4846c7
Revises: add_desktop_url_to_assistants
Create Date: 2025-09-18 13:40:49.896531

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "51f08d4846c7"
down_revision = "add_desktop_url_to_assistants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the existing foreign key first
    op.drop_constraint("fk_assistants_voices", "assistants", type_="foreignkey")

    # 2. Ensure provider column is non-nullable
    op.alter_column(
        "voices",
        "provider",
        existing_type=sa.VARCHAR(),
        nullable=False,
        existing_server_default=sa.text("'cartesia'::character varying"),
    )

    # 3. Rebuild primary key on voices to include provider
    op.drop_constraint("voices_pkey", "voices", type_="primary")
    op.create_primary_key("voices_pkey", "voices", ["user_id", "voice_id", "provider"])

    # 4. Add new column to assistants
    op.add_column("assistants", sa.Column("voice_provider", sa.String(), nullable=True))

    # 5. Populate existing assistants with default provider
    op.execute(
        """
        UPDATE assistants a
        SET voice_provider = 'elevenlabs'
        FROM voices v
        WHERE a.user_id = v.user_id AND a.voice_id = v.voice_id
    """
    )

    # 6. Rebuild the foreign key with the new composite key structure
    op.create_foreign_key(
        "fk_assistants_voices",
        "assistants",
        "voices",
        ["user_id", "voice_id", "voice_provider"],
        ["user_id", "voice_id", "provider"],
    )


def downgrade() -> None:
    # 1. Drop the new foreign key
    op.drop_constraint("fk_assistants_voices", "assistants", type_="foreignkey")

    # 2. Remove the voice_provider column from assistants
    op.drop_column("assistants", "voice_provider")

    # 3. Rebuild voices primary key back to (user_id, voice_id)
    op.drop_constraint("voices_pkey", "voices", type_="primary")
    op.create_primary_key("voices_pkey", "voices", ["user_id", "voice_id"])

    # 4. Restore the old foreign key (without provider)
    op.create_foreign_key(
        "fk_assistants_voices",
        "assistants",
        "voices",
        ["user_id", "voice_id"],
        ["user_id", "voice_id"],
    )

    # 5. Make provider nullable again
    op.alter_column(
        "voices",
        "provider",
        existing_type=sa.VARCHAR(),
        nullable=True,
        existing_server_default=sa.text("'cartesia'::character varying"),
    )
