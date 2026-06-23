"""Default voice handling for Coordinator assistants.

Every Coordinator ("Twin") starts with the canonical voice. ``before_insert``
ensures the per-user voice row exists in ``voices`` (the assistants table
carries a composite FK to it) and stamps that default voice onto the new
Coordinator row.

Existing rows are backfilled by the ``coordinator_fixed_voice`` migration.
The Console mirrors the default value in
``src/constants/assistants/approved_character_voices.ts``
(``coordinatorFixedVoiceId``).
"""

from sqlalchemy import event
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestra.db.models.orchestra_models import Assistant, Voice

COORDINATOR_VOICE_PROVIDER = "elevenlabs"
COORDINATOR_VOICE_ID = "iP95p4xoKVk53GoZ742B"
COORDINATOR_VOICE_NAME = "Friendly Flicker"
COORDINATOR_VOICE_DESCRIPTION = (
    "Natural, casual, and upbeat for grounded helper personalities."
)
COORDINATOR_VOICE_GENDER = "male"
COORDINATOR_VOICE_LANGUAGE = "en"


def ensure_coordinator_voice_row(connection, user_id: str) -> None:
    """Insert the canonical Coordinator voice for ``user_id`` if missing."""
    connection.execute(
        pg_insert(Voice.__table__)
        .values(
            voice_id=COORDINATOR_VOICE_ID,
            user_id=user_id,
            provider=COORDINATOR_VOICE_PROVIDER,
            name=COORDINATOR_VOICE_NAME,
            description=COORDINATOR_VOICE_DESCRIPTION,
            gender=COORDINATOR_VOICE_GENDER,
            language=COORDINATOR_VOICE_LANGUAGE,
            is_preset=True,
        )
        .on_conflict_do_nothing(),
    )


@event.listens_for(Assistant, "before_insert")
def _stamp_coordinator_voice_on_insert(mapper, connection, target) -> None:
    if not target.is_coordinator:
        return
    ensure_coordinator_voice_row(connection, target.user_id)
    target.voice_id = COORDINATOR_VOICE_ID
    target.voice_provider = COORDINATOR_VOICE_PROVIDER
