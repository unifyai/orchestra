"""The registration guards read the signer's origin, not the caller's.

``POST /admin/auth/register`` is called by Console's server, so every
attempt arrives from the same connection carrying the same HTTP client.
Three guards were reading that connection and so were describing
Console: the velocity limits keyed on its egress address, the bot
heuristic judged its user agent, and Turnstile was handed its peer.

The values now travel in the body, which introduces a failure mode worth
pinning as hard as the behaviour itself: while a deployment is mid-roll
the two sides disagree about sending them, and a guard that reads a
missing origin as hostile would refuse every signup for the duration.
"""

from __future__ import annotations

import pytest

from orchestra.db.dao.auth_dao import check_user_agent
from orchestra.web.api.utils.auth_rate_limiting import rate_limit_origin, subnet_of


def _console_request():
    """A request as it arrives from Console's server."""
    from types import SimpleNamespace

    return SimpleNamespace(
        headers={"x-forwarded-for": "10.4.0.9"},
        client=SimpleNamespace(host="10.4.0.9"),
    )


class TestVelocityLimitKey:
    """What an attempt is counted against."""

    def test_the_signer_is_counted_rather_than_console(self):
        """Keying on the connection puts the whole platform in one bucket."""
        assert (
            rate_limit_origin(_console_request(), client_ip="198.51.100.7")
            == "198.51.100.7"
        )

    def test_the_connection_is_the_fallback_when_nothing_was_forwarded(self):
        """No worse than the behaviour it replaces, so a rollout cannot lock signup."""
        assert rate_limit_origin(_console_request()) == "10.4.0.9"

    def test_a_subnet_key_collapses_the_signer_not_the_caller(self):
        assert (
            rate_limit_origin(
                _console_request(),
                client_ip="198.51.100.7",
                use_subnet=True,
            )
            == "198.51.100.0/24"
        )

    def test_ipv6_collapses_to_a_prefix(self):
        assert subnet_of("2001:db8:abcd:1234::1").endswith("::/48")


class TestBotHeuristicInput:
    """What the heuristic is given to judge."""

    def test_consoles_own_client_would_pass_anything(self):
        """The reason judging the connection was indistinguishable from not judging."""
        assert check_user_agent("axios/1.18.1") is True

    def test_a_real_automation_agent_is_still_caught(self):
        assert check_user_agent("python-requests/2.32") is False
        assert check_user_agent("curl/8.7.1") is False

    def test_headless_chrome_is_caught_as_it_actually_identifies_itself(self):
        """The agent sends "HeadlessChrome/120", not "Headless Chrome".

        A trailing word boundary never matches there, so the pattern
        named for this agent could not catch it. Nothing noticed while
        the heuristic was judging Console's HTTP client, which matched
        no pattern either way.
        """
        assert (
            check_user_agent(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) HeadlessChrome/120.0.0.0 Safari/537.36",
            )
            is False
        )

    def test_an_ordinary_chrome_is_not_caught_by_that_widening(self):
        assert (
            check_user_agent(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            is True
        )

    def test_an_ordinary_browser_passes(self):
        assert (
            check_user_agent(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            )
            is True
        )

    def test_an_absent_agent_reads_as_hostile(self):
        """Which is why the endpoint must not judge one that never arrived.

        This is the whole reason the call is guarded rather than made
        unconditionally: a deployment that has not yet started forwarding
        the agent would otherwise have every signup refused.
        """
        assert check_user_agent(None) is False
        assert check_user_agent("") is False


@pytest.mark.anyio
class TestRegistrationSurvivesAMissingOrigin:
    """A half-rolled deployment must not take signup down."""

    async def test_registration_is_reachable_without_provenance(self, client):
        """No 403 from the bot guard when no agent was forwarded.

        The response may be anything the flow legitimately produces --
        what must not appear is the suspicious-request refusal, which
        would mean an absent agent had been judged.
        """
        resp = await client.post(
            "/v0/admin/auth/register",
            json={
                "email": "origin_absent@example.com",
                "password": "Sufficiently-Long-Passw0rd!",
            },
        )

        assert resp.status_code != 403
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            assert detail.get("error") != "suspicious_request"
