"""Signup provenance capture for abuse correlation.

Free credits are Console-only, so credit farming no longer surfaces on
the raw-API channel an earlier sweep watched. What remains is only
separable by correlating accounts with each other, and that needs to
know where each signup came from.

Two values are recorded, both deliberately coarse:

* the caller IP, as the referral flow already records it; and
* a salted hash of the user agent, because the cluster sweep compares it
  for equality and never reads it, so keeping the raw string would be
  more identifying than the job requires.

Neither is a strong identifier on its own — an IP is shared by offices,
VPNs and carriers, and a user agent is shared by everyone on the same
browser build. That is why they feed a *cluster* signal, where the
evidence is a burst of never-paid accounts sharing an origin, and never
an individual freeze decision.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from fastapi import Request

from orchestra.settings import settings

#: Header set by the ingress/load balancer. ``request.client.host`` is the
#: proxy behind Cloud Run, so the left-most forwarded hop is the real one.
_FORWARDED_FOR = "x-forwarded-for"

#: Bound on what we will store, so a hostile header cannot bloat the row.
_MAX_IP_LENGTH = 45  # an IPv6 address with a zone index


def client_ip(request: Request) -> Optional[str]:
    """The caller's IP, preferring the left-most forwarded hop.

    Returns ``None`` rather than a placeholder when nothing usable is
    present: a column full of ``"unknown"`` would cluster every such
    signup together and manufacture false evidence.
    """
    forwarded = request.headers.get(_FORWARDED_FOR, "")
    candidate = forwarded.split(",")[0].strip()
    if not candidate and request.client:
        candidate = (request.client.host or "").strip()
    if not candidate:
        return None
    return candidate[:_MAX_IP_LENGTH]


def user_agent_hash(request: Request) -> Optional[str]:
    """Salted hash of the request's user agent, or ``None`` if absent.

    Salted with an existing server secret so the digest is not reversible
    by rainbow table against the small space of common user agents.
    """
    raw = (request.headers.get("user-agent") or "").strip()
    if not raw:
        return None

    salt = settings.email_verify_token_secret or settings.mfa_encryption_key or ""
    return hmac.new(
        salt.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def signup_provenance(request: Optional[Request]) -> dict:
    """Kwargs for :meth:`UserDAO.create` describing where a signup came from.

    Always returns both keys so callers can splat it unconditionally;
    values are ``None`` when the request cannot supply them.
    """
    if request is None:
        return {"signup_ip": None, "signup_user_agent_hash": None}
    return {
        "signup_ip": client_ip(request),
        "signup_user_agent_hash": user_agent_hash(request),
    }
