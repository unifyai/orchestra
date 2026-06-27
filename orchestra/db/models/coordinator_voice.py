"""Default voice handling for Coordinator assistants.

A Coordinator ("Twin") is a selectable-voice droid like any other; it simply
gets a sensible *default* voice when one was not chosen at creation.
``before_insert`` ensures the default voice row exists in ``voices`` (the
assistants table carries a composite FK to it) and fills the default only when
the new Coordinator row has no voice of its own. The voice remains fully
overridable afterwards via the normal assistant update path.

The Console mirrors the default value in
``src/constants/assistants/approved_character_voices.ts``
(``coordinatorDefaultVoiceId``).
"""

from sqlalchemy import event
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestra.db.models.orchestra_models import Assistant, Voice

COORDINATOR_DEFAULT_VOICE_PROVIDER = "elevenlabs"
COORDINATOR_DEFAULT_VOICE_ID = "iP95p4xoKVk53GoZ742B"
COORDINATOR_DEFAULT_VOICE_NAME = "Friendly Flicker"
COORDINATOR_DEFAULT_VOICE_DESCRIPTION = (
    "Natural, casual, and upbeat for grounded helper personalities."
)
COORDINATOR_DEFAULT_VOICE_GENDER = "male"
COORDINATOR_DEFAULT_VOICE_LANGUAGE = "en"


def ensure_coordinator_voice_row(connection, user_id: str) -> None:
    """Insert the default Coordinator voice for ``user_id`` if missing."""
    connection.execute(
        pg_insert(Voice.__table__)
        .values(
            voice_id=COORDINATOR_DEFAULT_VOICE_ID,
            user_id=user_id,
            provider=COORDINATOR_DEFAULT_VOICE_PROVIDER,
            name=COORDINATOR_DEFAULT_VOICE_NAME,
            description=COORDINATOR_DEFAULT_VOICE_DESCRIPTION,
            gender=COORDINATOR_DEFAULT_VOICE_GENDER,
            language=COORDINATOR_DEFAULT_VOICE_LANGUAGE,
            is_preset=True,
        )
        .on_conflict_do_nothing(),
    )


@event.listens_for(Assistant, "before_insert")
def _stamp_coordinator_voice_on_insert(mapper, connection, target) -> None:
    if not target.is_coordinator:
        return
    ensure_coordinator_voice_row(connection, target.user_id)
    # Fill the default only when no voice was chosen. Coordinators are otherwise
    # free to use any registered voice, just like every other droid.
    if not target.voice_id:
        target.voice_id = COORDINATOR_DEFAULT_VOICE_ID
        target.voice_provider = COORDINATOR_DEFAULT_VOICE_PROVIDER
