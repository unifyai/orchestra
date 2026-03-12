"""
Live Cloudflare Turnstile integration tests.

These tests call the **real** Cloudflare siteverify API using the
official test secret keys documented at:
https://developers.cloudflare.com/turnstile/troubleshooting/testing/

They are **not** run in CI — the ``require_turnstile`` fixture skips
the test when network access to Cloudflare is unavailable.

Test secret keys (server-side):
  - Always passes:  1x0000000000000000000000000000000AA
  - Always fails:   2x0000000000000000000000000000000AA
  - Token already spent: 3x0000000000000000000000000000000AA

Matching dummy response tokens (client-side):
  - XXXX.DUMMY.TOKEN.XXXX  (works with the "always passes" secret key)
"""

from unittest.mock import patch

import httpx
import pytest

_MODULE = "orchestra.db.dao.auth_dao"

# Cloudflare test keys (documented, safe to commit)
_ALWAYS_PASS_SECRET = "1x0000000000000000000000000000000AA"
_ALWAYS_FAIL_SECRET = "2x0000000000000000000000000000000AA"
_ALREADY_SPENT_SECRET = "3x0000000000000000000000000000000AA"
_DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"


def _cloudflare_reachable() -> bool:
    """Return True if the Cloudflare Turnstile API is reachable."""
    try:
        resp = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": _ALWAYS_PASS_SECRET, "response": _DUMMY_TOKEN},
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture
def require_turnstile():
    """Skip the test when Cloudflare is not reachable."""
    if not _cloudflare_reachable():
        pytest.skip("Cloudflare Turnstile API not reachable")


class TestLiveTurnstile:
    """Tests that hit the real Cloudflare Turnstile siteverify endpoint."""

    @pytest.mark.anyio
    async def test_always_pass_secret(self, require_turnstile):
        """The 'always passes' test secret accepts the dummy token."""
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        with patch(f"{_MODULE}.settings") as ms:
            ms.turnstile_secret_key = _ALWAYS_PASS_SECRET
            result = await verify_turnstile_token(_DUMMY_TOKEN)

        assert result is True

    @pytest.mark.anyio
    async def test_always_fail_secret(self, require_turnstile):
        """The 'always fails' test secret rejects the dummy token."""
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        with patch(f"{_MODULE}.settings") as ms:
            ms.turnstile_secret_key = _ALWAYS_FAIL_SECRET
            result = await verify_turnstile_token(_DUMMY_TOKEN)

        assert result is False

    @pytest.mark.anyio
    async def test_already_spent_secret(self, require_turnstile):
        """The 'token already spent' test secret rejects the dummy token."""
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        with patch(f"{_MODULE}.settings") as ms:
            ms.turnstile_secret_key = _ALREADY_SPENT_SECRET
            result = await verify_turnstile_token(_DUMMY_TOKEN)

        assert result is False
