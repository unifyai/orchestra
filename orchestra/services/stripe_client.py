"""
Central Stripe wrapper.

• In production we import the official `stripe` SDK and inject the API key
  once, then re-export the resources the app uses.
• When the SDK is *not* installed (CI or offline dev) we expose lightweight
  stub classes so imports succeed – tests normally monkey-patch them.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

try:
    import stripe as _stripe  # production path

    # One place to inject creds / config
    try:  # settings may be absent in some unit-test contexts
        from orchestra.settings import settings  # type: ignore

        _stripe.api_key = getattr(settings, "stripe_key", _stripe.api_key)
    except Exception:  # pragma: no cover
        pass

    # Re-export the pieces the codebase uses
    Invoice = _stripe.Invoice
    InvoiceItem = _stripe.InvoiceItem

except ModuleNotFoundError:  # ─────────────────────────────── test stub

    class _StubResource:  # noqa: D101 – minimal fake
        @staticmethod
        def create(*_a: Any, **_kw: Any):  # noqa: D401
            raise RuntimeError(
                "Stripe SDK not installed – this stub should be monkey-patched "
                "inside unit-tests.",
            )

    Invoice = InvoiceItem = _StubResource  # type: ignore

# Provide module-level attributes so `import … as stripe` works
stripe = SimpleNamespace(Invoice=Invoice, InvoiceItem=InvoiceItem)

# What `from orchestra.services import stripe_client as stripe` exports
__all__ = ["Invoice", "InvoiceItem", "stripe"]
