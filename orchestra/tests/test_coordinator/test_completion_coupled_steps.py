"""Reset coupling for onboarding steps.

Resetting a WhatsApp/phone quiz step must reset that channel's task steps but
keep the saved contact detail ("Add your number") intact, so the user never has
to re-enter their number to redo the channel. Resetting the detail step itself
still cascades to the whole channel.
"""

from __future__ import annotations

from orchestra.services import onboarding_graph as graph


def test_resetting_whatsapp_task_keeps_number() -> None:
    coupled = set(graph.completion_coupled_steps("whatsapp-message"))

    assert "whatsapp-number" not in coupled
    # Sibling WhatsApp quiz tasks still reset together.
    assert {
        "whatsapp-message-reference",
        "whatsapp-message",
        "whatsapp-call-reference",
        "whatsapp-call",
    } <= coupled


def test_resetting_phone_task_keeps_number() -> None:
    coupled = set(graph.completion_coupled_steps("phone-call"))

    assert "phone-number" not in coupled
    assert {
        "sms-reference",
        "sms-message",
        "phone-call-reference",
        "phone-call",
    } <= coupled


def test_resetting_number_step_still_cascades_full_channel() -> None:
    coupled = set(graph.completion_coupled_steps("whatsapp-number"))

    assert "whatsapp-number" in coupled
    assert {
        "whatsapp-message-reference",
        "whatsapp-message",
        "whatsapp-call-reference",
        "whatsapp-call",
    } <= coupled


def test_channel_reset_does_not_leak_into_other_channels() -> None:
    coupled = set(graph.completion_coupled_steps("whatsapp-message"))

    assert "phone-number" not in coupled
    assert "sms-message" not in coupled
    assert "phone-call" not in coupled
