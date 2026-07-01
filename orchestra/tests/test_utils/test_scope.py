"""Unit tests for kernel ownership-scope derivation from context names."""

import pytest

from orchestra.db.scope import OwnerScope, owner_from_context_name


@pytest.mark.parametrize(
    ("name", "scope", "owner_id"),
    [
        # Per-assistant contexts: {user_id}/{agent_id}/<Manager>/...
        ("default/0/Contacts", OwnerScope.ASSISTANT, 0),
        ("a1b2-uuid/42/Knowledge/Facts", OwnerScope.ASSISTANT, 42),
        # Shared team contexts: Teams/{team_id}/...
        ("Teams/7/Contacts", OwnerScope.TEAM, 7),
        # Test contexts carry a variable-depth tests/<...> root.
        ("tests/run123/default/5/Data", OwnerScope.ASSISTANT, 5),
        ("tests/run-x/y/Teams/9/Contacts", OwnerScope.TEAM, 9),
        # System / builtins.
        ("Builtins/Guidance", OwnerScope.SYSTEM, None),
        ("Functions", OwnerScope.SYSTEM, None),
        ("", OwnerScope.SYSTEM, None),
    ],
)
def test_owner_from_context_name(name, scope, owner_id):
    owner = owner_from_context_name(name)
    assert owner.scope == scope
    assert owner.owner_id == owner_id


def test_team_prefix_not_misread_as_assistant():
    # The team_id integer must not be picked up by the assistant scan.
    assert owner_from_context_name("Teams/13/Knowledge").scope == OwnerScope.TEAM
