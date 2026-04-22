"""ORM-level write-once enforcement for ``Assistant.hive_id``.

``hive_id`` is a provisioning-time field: it may be set when the row is first
created but may not be mutated afterwards. The ``@validates`` guard on the
``Assistant`` model raises ``ValueError`` before SQLAlchemy reaches the
database, so any attempt to change a non-NULL value surfaces at flush time
regardless of how the mutation is attempted.
"""

from __future__ import annotations

import pytest

from orchestra.db.models.orchestra_models import Assistant, Hive, Organization

# Seeded by orchestra/tests/seeding.sql.
_USER_A = "user1"
_USER_B = "user2"
_USER_C = "user3"


def _seed_hive(dbsession, *, owner_id: str, name: str) -> Hive:
    org = Organization(name=name, owner_id=owner_id)
    dbsession.add(org)
    dbsession.flush()
    hive = Hive(organization_id=org.id, name=name)
    dbsession.add(hive)
    dbsession.flush()
    return hive


def test_hive_id_can_be_set_at_creation(dbsession):
    """An assistant may be created with a hive_id."""

    hive = _seed_hive(dbsession, owner_id=_USER_A, name="WriteOnceCreate")
    assistant = Assistant(
        user_id=_USER_A,
        first_name="A",
        surname="B",
        hive_id=hive.hive_id,
    )
    dbsession.add(assistant)
    dbsession.flush()
    assert assistant.hive_id == hive.hive_id


def test_hive_id_mutation_to_different_value_raises(dbsession):
    """Changing hive_id from one non-NULL value to another must raise before flush."""

    hive = _seed_hive(dbsession, owner_id=_USER_B, name="WriteOnceMutate")
    assistant = Assistant(
        user_id=_USER_B,
        first_name="A",
        surname="B",
        hive_id=hive.hive_id,
    )
    dbsession.add(assistant)
    dbsession.flush()

    # The validator fires on attribute set; no DB round-trip needed.
    with pytest.raises(ValueError, match="write-once"):
        assistant.hive_id = hive.hive_id + 999


def test_hive_id_mutation_to_null_raises(dbsession):
    """Clearing hive_id back to NULL after it was set must raise before flush."""

    hive = _seed_hive(dbsession, owner_id=_USER_C, name="WriteOnceNull")
    assistant = Assistant(
        user_id=_USER_C,
        first_name="A",
        surname="B",
        hive_id=hive.hive_id,
    )
    dbsession.add(assistant)
    dbsession.flush()

    with pytest.raises(ValueError, match="write-once"):
        assistant.hive_id = None
