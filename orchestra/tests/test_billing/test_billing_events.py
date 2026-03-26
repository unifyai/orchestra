"""Tests for billing event publishing (billing_events.py).

Verifies:
- Legacy ``publish_if_credits_exhausted`` / ``publish_if_credits_restored``
  fire only when balance crosses zero.
- Session-tracked ``track_balance_before`` / ``track_balance_after`` publish
  the correct event on commit, including auto-recharge scenarios.
- The publisher is fire-and-forget: failures are swallowed.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from orchestra.lib.billing_events import (
    _LISTENER_KEY,
    _SESSION_KEY,
    _flush_billing_events,
    publish_if_credits_exhausted,
    publish_if_credits_restored,
    track_balance_after,
    track_balance_before,
)


@pytest.fixture(autouse=True)
def _reset_publisher():
    """Ensure the publisher singleton is reset between tests."""
    import orchestra.lib.billing_events as mod

    mod._PUBLISHER = None
    mod._PUBLISHER_INIT_ATTEMPTED = False
    yield
    mod._PUBLISHER = None
    mod._PUBLISHER_INIT_ATTEMPTED = False


def _make_mock_publisher():
    publisher = MagicMock()
    publisher.topic_path.return_value = "projects/test/topics/billing-account-1"
    return publisher


def _make_mock_session():
    """Create a minimal mock session with an info dict."""
    session = MagicMock()
    session.info = {}
    return session


# =========================================================================
# Legacy helpers
# =========================================================================


class TestPublishIfCreditsExhausted:
    """credits_exhausted should fire only on positive -> non-positive crossing."""

    def test_fires_when_crossing_zero_downward(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_exhausted(
                billing_account_id=1,
                previous_balance=5.0,
                new_balance=-0.50,
            )
            publisher = mock_get.return_value
            publisher.publish.assert_called_once()
            payload_bytes = publisher.publish.call_args[0][1]
            assert b"credits_exhausted" in payload_bytes

    def test_fires_when_balance_reaches_exactly_zero(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_exhausted(
                billing_account_id=1,
                previous_balance=2.0,
                new_balance=0.0,
            )
            mock_get.return_value.publish.assert_called_once()

    def test_does_not_fire_when_staying_positive(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_exhausted(
                billing_account_id=1,
                previous_balance=10.0,
                new_balance=5.0,
            )
            mock_get.return_value.publish.assert_not_called()

    def test_does_not_fire_when_staying_negative(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_exhausted(
                billing_account_id=1,
                previous_balance=-2.0,
                new_balance=-5.0,
            )
            mock_get.return_value.publish.assert_not_called()

    def test_accepts_decimal_values(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_exhausted(
                billing_account_id=1,
                previous_balance=Decimal("1.50"),
                new_balance=Decimal("-0.25"),
            )
            mock_get.return_value.publish.assert_called_once()

    def test_swallows_publish_failure(self):
        publisher = _make_mock_publisher()
        publisher.publish.side_effect = Exception("network error")
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=publisher,
        ):
            publish_if_credits_exhausted(
                billing_account_id=1,
                previous_balance=5.0,
                new_balance=-1.0,
            )


class TestPublishIfCreditsRestored:
    """credits_restored should fire only on non-positive -> positive crossing."""

    def test_fires_when_crossing_zero_upward(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_restored(
                billing_account_id=1,
                previous_balance=-2.0,
                new_balance=25.0,
            )
            publisher = mock_get.return_value
            publisher.publish.assert_called_once()
            payload_bytes = publisher.publish.call_args[0][1]
            assert b"credits_restored" in payload_bytes

    def test_fires_when_crossing_from_zero_to_positive(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_restored(
                billing_account_id=1,
                previous_balance=0.0,
                new_balance=10.0,
            )
            mock_get.return_value.publish.assert_called_once()

    def test_does_not_fire_when_staying_positive(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_restored(
                billing_account_id=1,
                previous_balance=5.0,
                new_balance=30.0,
            )
            mock_get.return_value.publish.assert_not_called()

    def test_does_not_fire_when_staying_negative(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            publish_if_credits_restored(
                billing_account_id=1,
                previous_balance=-10.0,
                new_balance=-5.0,
            )
            mock_get.return_value.publish.assert_not_called()

    def test_no_publisher_available(self):
        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=None,
        ):
            publish_if_credits_restored(
                billing_account_id=1,
                previous_balance=-5.0,
                new_balance=10.0,
            )


# =========================================================================
# Session-tracked balance events
# =========================================================================


class TestSessionTrackedEvents:
    """Tests for track_balance_before / track_balance_after + after_commit."""

    @pytest.fixture(autouse=True)
    def _patch_listener(self):
        """Skip actual SQLAlchemy event registration (mock sessions don't support it)."""
        with patch("orchestra.lib.billing_events._ensure_after_commit_listener"):
            yield

    def test_exhausted_event_on_flush(self):
        """Deduction crossing zero should publish credits_exhausted."""
        session = _make_mock_session()
        track_balance_before(session, 1, Decimal("5.00"))
        track_balance_after(session, 1, Decimal("-0.50"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session)
            publisher = mock_get.return_value
            publisher.publish.assert_called_once()
            assert b"credits_exhausted" in publisher.publish.call_args[0][1]

    def test_restored_event_on_flush(self):
        """Addition crossing zero should publish credits_restored."""
        session = _make_mock_session()
        track_balance_before(session, 1, Decimal("-2.00"))
        track_balance_after(session, 1, Decimal("25.00"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session)
            publisher = mock_get.return_value
            publisher.publish.assert_called_once()
            assert b"credits_restored" in publisher.publish.call_args[0][1]

    def test_no_event_when_staying_positive(self):
        session = _make_mock_session()
        track_balance_before(session, 1, Decimal("50.00"))
        track_balance_after(session, 1, Decimal("45.00"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session)
            mock_get.return_value.publish.assert_not_called()

    def test_no_event_when_staying_negative(self):
        session = _make_mock_session()
        track_balance_before(session, 1, Decimal("-5.00"))
        track_balance_after(session, 1, Decimal("-10.00"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session)
            mock_get.return_value.publish.assert_not_called()

    def test_auto_recharge_prevents_exhausted_event(self):
        """Deduction goes negative, then auto-recharge restores — no event."""
        session = _make_mock_session()
        # DAO deduct_credits records initial snapshot
        track_balance_before(session, 1, Decimal("5.00"))
        track_balance_after(session, 1, Decimal("-0.50"))
        # auto-recharge adds credits (updates final balance)
        track_balance_after(session, 1, Decimal("24.50"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session)
            mock_get.return_value.publish.assert_not_called()

    def test_first_previous_balance_wins(self):
        """Multiple track_balance_before calls keep only the first snapshot."""
        session = _make_mock_session()
        track_balance_before(session, 1, Decimal("5.00"))
        # second call should be a no-op for previous
        track_balance_before(session, 1, Decimal("-0.50"))
        track_balance_after(session, 1, Decimal("-1.00"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session)
            publisher = mock_get.return_value
            publisher.publish.assert_called_once()
            assert b"credits_exhausted" in publisher.publish.call_args[0][1]

    def test_multiple_accounts_independent(self):
        """Events for different billing accounts are tracked independently."""
        session = _make_mock_session()
        track_balance_before(session, 1, Decimal("5.00"))
        track_balance_after(session, 1, Decimal("-0.50"))
        track_balance_before(session, 2, Decimal("-3.00"))
        track_balance_after(session, 2, Decimal("22.00"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session)
            publisher = mock_get.return_value
            assert publisher.publish.call_count == 2

    def test_flush_clears_session_info(self):
        session = _make_mock_session()
        track_balance_before(session, 1, Decimal("5.00"))
        track_balance_after(session, 1, Decimal("-0.50"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ):
            _flush_billing_events(session)

        assert _SESSION_KEY not in session.info
        assert _LISTENER_KEY not in session.info

    def test_track_balance_after_without_before(self):
        """track_balance_after without prior before should still work."""
        session = _make_mock_session()
        track_balance_after(session, 1, Decimal("5.00"))

        assert 1 in session.info[_SESSION_KEY]
        assert session.info[_SESSION_KEY][1]["previous"] == 5.0
        assert session.info[_SESSION_KEY][1]["final"] == 5.0

    def test_concurrent_sessions_are_isolated(self):
        """Each session tracks balances independently (simulates parallel requests)."""
        session_a = _make_mock_session()
        session_b = _make_mock_session()

        track_balance_before(session_a, 1, Decimal("10.00"))
        track_balance_before(session_b, 1, Decimal("2.00"))
        track_balance_after(session_a, 1, Decimal("2.00"))
        track_balance_after(session_b, 1, Decimal("-3.00"))

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session_a)
            mock_get.return_value.publish.assert_not_called()

        with patch(
            "orchestra.lib.billing_events._get_publisher",
            return_value=_make_mock_publisher(),
        ) as mock_get:
            _flush_billing_events(session_b)
            publisher = mock_get.return_value
            publisher.publish.assert_called_once()
            assert b"credits_exhausted" in publisher.publish.call_args[0][1]
