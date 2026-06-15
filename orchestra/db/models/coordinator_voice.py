"""Fixed-voice invariant for Coordinator assistants.

Every Coordinator ("Marty") speaks with one canonical voice. The invariant
is enforced at the ORM flush boundary so that no API endpoint, service, or
future code path can persist a Coordinator row with any other voice:

* ``before_insert`` ensures the canonical per-user row exists in ``voices``
  (the assistants table carries a composite FK to it) and stamps the fixed
  voice onto the new Coordinator row.
* ``before_update`` re-stamps the fixed voice, silently overriding any
  attempt to change it.

Existing rows are backfilled by the ``coordinator_fixed_voice`` migration.
The Console mirrors these values in
``src/constants/assistants/approved_character_voices.ts``
(``coordinatorFixedVoiceId``) for display purposes only — this module is
the source of truth.
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


@event.listens_for(Assistant, "before_update")
def _stamp_coordinator_voice_on_update(mapper, connection, target) -> None:
    if not target.is_coordinator:
        return
    target.voice_id = COORDINATOR_VOICE_ID
    target.voice_provider = COORDINATOR_VOICE_PROVIDER
