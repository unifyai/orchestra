"""Primary next-target selection for onboarding nudges."""

from __future__ import annotations

from orchestra.services import onboarding_graph as graph


def _ids(*step_ids: str) -> set[str]:
    return set(step_ids)


def test_primary_defaults_to_first_available_in_graph_order() -> None:
    """With no progress anchor, the topmost available step wins."""
    available = _ids("whatsapp-number", "phone-number")
    completed: set[str] = {"email-reference", "email-reply"}

    primary = graph.select_primary_next_target_id(
        available,
        completed=completed,
        active_step_id=None,
    )

    assert primary == "whatsapp-number"


def test_primary_continues_phone_track_after_phone_setup() -> None:
    """Completing phone setup should nudge toward SMS, not WhatsApp."""
    available = _ids("whatsapp-number", "sms-reference")
    completed = {"email-reference", "email-reply", "phone-number"}

    primary = graph.select_primary_next_target_id(
        available,
        completed=completed,
        active_step_id=None,
    )

    assert primary == "sms-reference"


def test_primary_continues_whatsapp_track_after_whatsapp_setup() -> None:
    """Completing WhatsApp setup should nudge toward the WhatsApp message clue."""
    available = _ids("whatsapp-message-reference", "phone-number")
    completed = {"email-reference", "email-reply", "whatsapp-number"}

    primary = graph.select_primary_next_target_id(
        available,
        completed=completed,
        active_step_id="whatsapp-number",
    )

    assert primary == "whatsapp-message-reference"


def test_active_incomplete_step_stays_primary() -> None:
    """Mid-flow on a setup row, nudge finishing that row first."""
    available = _ids("phone-number", "whatsapp-number")

    primary = graph.select_primary_next_target_id(
        available,
        completed={"email-reference", "email-reply"},
        active_step_id="phone-number",
    )

    assert primary == "phone-number"


def test_order_next_targets_puts_primary_first() -> None:
    targets = [
        {"id": "whatsapp-number", "title": "Add your WhatsApp number"},
        {"id": "sms-reference", "title": "Trigger SMS message from T-W1N"},
    ]
    ordered = graph.order_next_targets(
        targets,
        completed={"email-reference", "email-reply", "phone-number"},
        active_step_id=None,
    )

    assert [target["id"] for target in ordered] == [
        "sms-reference",
        "whatsapp-number",
    ]
