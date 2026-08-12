"""Signup provenance capture for abuse correlation.

Free credits are Console-only, so credit farming no longer surfaces on
the raw-API channel an earlier sweep watched. What remains is only
separable by correlating accounts with each other, and that needs to
know where each signup came from.

**The values arrive explicitly, never from the request.** Every signup
reaches Orchestra through Console's Next.js server on an admin-key
endpoint, so the transport request describes Console: one constant HTTP
client user agent on every signup, and Console's egress IP. Reading it
does not merely lose the signal, it inverts it — every hosted account
collects into a single shared-origin group that the cluster sweep would
read as a ring of strangers. Console holds the browser's request and
passes what it saw; this module only normalises and hashes.

Two values are recorded, both deliberately coarse:

* the client IP as Console observed it; and
* a salted hash of the user agent, because nothing reads it back and
  keeping the raw string would be more identifying than the job needs.

Neither is a strong identifier. An IP is shared by offices, VPNs and
carriers; a user agent is shared by everyone on the same browser build.
The sweep clusters on the IP alone for exactly that reason — a browser
build is far too coarse to be evidence of anything, however many
accounts share one — and the stored hash is corroboration for a human
reading a match, not a key.

The IP is also only as good as what Console could see: the left-most
forwarded hop is supplied by the caller, so a determined signer can
choose the address recorded against them. That bounds this to
unsophisticated farming, and is a reason to read a match before acting
on it rather than a reason to record nothing.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from orchestra.settings import settings

#: Bound on what we will store, so a hostile value cannot bloat the row.
_MAX_IP_LENGTH = 45  # an IPv6 address with a zone index


def normalised_ip(ip: Optional[str]) -> Optional[str]:
    """The client IP as recorded, or ``None`` when nothing usable came.

    ``None`` rather than a placeholder: a column full of ``"unknown"``
    would cluster every such signup together and manufacture evidence.
    """
    candidate = (ip or "").strip()
    if not candidate:
        return None
    return candidate[:_MAX_IP_LENGTH]


def user_agent_hash(user_agent: Optional[str]) -> Optional[str]:
    """Salted hash of a user agent, or ``None`` if absent.

    Salted with an existing server secret so the digest is not reversible
    by rainbow table against the small space of common user agents.
    """
    raw = (user_agent or "").strip()
    if not raw:
        return None

    salt = settings.email_verify_token_secret or settings.mfa_encryption_key or ""
    return hmac.new(
        salt.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def signup_provenance(
    ip: Optional[str],
    user_agent: Optional[str],
) -> dict:
    """Kwargs for :meth:`UserDAO.create` describing where a signup came from.

    Always returns both keys so callers can splat it unconditionally;
    values are ``None`` when Console could not supply them.
    """
    return {
        "signup_ip": normalised_ip(ip),
        "signup_user_agent_hash": user_agent_hash(user_agent),
    }
