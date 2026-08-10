"""Label the model catalogue with what the spend boundary will refuse.

A never-paid account cannot spend on the premium providers: the runtime's
limit-check hook refuses the call at the moment of spend, on every surface
including the Console. The catalogue endpoints render the picker a user
chooses from, so without this they offer models that are accepted, saved,
and only refused later, when the user sends a message and gets a refusal
they had no way to anticipate.

**Mirrored decision.** Enforcement is ``_payment_gated`` /
``_provider_of`` in ``unify.spending_limits``; this module exists to
predict it, not to add a second rule. It deliberately reproduces that
logic — including reading the same ``PAYMENT_GATED_PROVIDERS`` variable
and splitting the endpoint on its *last* ``@`` — because a picker that
disagrees with the spend boundary is worse than one that says nothing:
it either promises a model that will be refused, or withholds one that
would have worked. Any change to the rule there must be made here in the
same changeset.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from orchestra.settings import settings

#: Spelled the way a reader writes them, not the way the endpoint suffix is
#: matched. An unmapped provider falls back to its raw slug so a newly-gated
#: one stays readable rather than blocking on an entry here.
_PROVIDER_DISPLAY_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "vertex-ai": "Vertex AI",
    "deepseek": "DeepSeek",
}


def payment_gated_providers() -> frozenset[str]:
    """Providers a never-paid account may not spend on."""
    return frozenset(
        p.strip().lower()
        for p in settings.payment_gated_providers.split(",")
        if p.strip()
    )


def provider_of(endpoint: Optional[str]) -> Optional[str]:
    """Extract the provider from a UniLLM ``model@provider`` endpoint.

    The model half may itself contain ``/`` (``openai/gpt-5.6-terra``), so
    the provider is the trailing segment after the last ``@``. Returns
    ``None`` for a bare model name, and for the system-default row whose
    model is ``None`` — both route by a default chosen elsewhere rather
    than naming a provider here.
    """
    if not endpoint:
        return None
    _, sep, provider = endpoint.rpartition("@")
    if not sep:
        return None
    return provider.strip().lower() or None


def provider_label(provider: str) -> str:
    """How a provider is spelled when the user reads it."""
    return _PROVIDER_DISPLAY_NAMES.get(provider, provider)


def account_never_paid(
    session: Session,
    user_id: str,
    organization_id: Optional[int],
) -> bool:
    """Whether the spend boundary will treat this account as never having paid.

    Reuses ``trial_gate_fields`` — the payload the runtime's limit check
    consumes — rather than re-deriving the condition, so the exemptions for
    internal accounts and admin-granted free trials cannot drift apart from
    what is actually enforced.
    """
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.lib.trial_subscription import trial_gate_fields

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    return bool(trial_gate_fields(session, ba)["never_paid"])


def payment_gate_reason(endpoint: Optional[str], *, never_paid: bool) -> Optional[str]:
    """Why this model is unselectable, or ``None`` if it is selectable.

    Written for a single line under a greyed row, so it states the
    condition and stops; the full remedy belongs in the refusal the user
    gets at spend time, which has room for it.
    """
    if not never_paid:
        return None
    provider = provider_of(endpoint)
    if provider is None or provider not in payment_gated_providers():
        return None
    return (
        f"{provider_label(provider)} models unlock after this account's "
        "first payment"
    )
