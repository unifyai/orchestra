"""Backfill missing default voice on Coordinator assistants.

The ``before_insert`` listener stamps Field Signal on new Coordinators, but
rows created while that module was not imported at runtime can remain with a
NULL voice. This heals every Coordinator that still has no ``voice_id``.

Revision ID: coord_default_voice_backfill
Revises: tune_embedding_queue_autovacuum
Create Date: 2026-07-08 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "coord_default_voice_backfill"
down_revision = "tune_embedding_queue_autovacuum"
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
          AND a.voice_id IS NULL
        ON CONFLICT DO NOTHING
        """,
    )
    op.execute(
        f"""
        UPDATE assistants
        SET voice_id = '{_VOICE_ID}',
            voice_provider = '{_VOICE_PROVIDER}'
        WHERE is_coordinator IS TRUE
          AND voice_id IS NULL
        """,
    )


def downgrade() -> None:
    pass
