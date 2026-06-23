"""Backfill the default Coordinator voice.

Coordinators start with one canonical voice. This migration heals existing rows:
it registers the canonical voice for every Coordinator owner (the assistants
table carries a composite FK into ``voices``) and points every Coordinator at it.

Revision ID: coordinator_fixed_voice
Revises: universal_coordinator_phone
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "coordinator_fixed_voice"
down_revision = "universal_coordinator_phone"
branch_labels = None
depends_on = None

_VOICE_ID = "iP95p4xoKVk53GoZ742B"
_VOICE_PROVIDER = "elevenlabs"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO voices (
            voice_id, user_id, provider, name, description,
            gender, language, is_preset
        )
        SELECT DISTINCT
            '{_VOICE_ID}',
            a.user_id,
            '{_VOICE_PROVIDER}',
            'Friendly Flicker',
            'Natural, casual, and upbeat for grounded helper personalities.',
            'male',
            'en',
            true
        FROM assistants a
        WHERE a.is_coordinator IS TRUE
        ON CONFLICT DO NOTHING
        """,
    )
    op.execute(
        f"""
        UPDATE assistants
        SET voice_id = '{_VOICE_ID}', voice_provider = '{_VOICE_PROVIDER}'
        WHERE is_coordinator IS TRUE
          AND (voice_id IS DISTINCT FROM '{_VOICE_ID}'
               OR voice_provider IS DISTINCT FROM '{_VOICE_PROVIDER}')
        """,
    )


def downgrade() -> None:
    # Data backfill only — nothing structural to revert.
    pass
