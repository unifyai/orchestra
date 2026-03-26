"""Publish billing lifecycle events to GCP Pub/Sub.

Events are thin signals ("balance crossed zero") that allow real-time
subscribers (e.g. the console SSE stream) to react instantly instead of
polling.  The Pub/Sub message is fire-and-forget; the authoritative
balance always comes from the DB via HTTP.

Topic naming: ``billing-account-{billing_account_id}{env_suffix}``
Thread attribute: ``billing_event`` (used by subscriber filters).

Usage
-----
Credit-mutation code (DAO, lib helpers) calls ``track_balance_before``
before changing credits and ``track_balance_after`` after.  Events are
published automatically when the session commits, so view-level code
never needs to touch Pub/Sub directly.
"""

import json
import logging
import os
from decimal import Decimal
from typing import Union

from sqlalchemy import event as sa_event

logger = logging.getLogger(__name__)

_PUBLISHER = None
_PUBLISHER_INIT_ATTEMPTED = False

_SESSION_KEY = "_billing_balance_snapshots"
_LISTENER_KEY = "_billing_events_listener"

_known_topics: set[str] = set()


# =============================================================================
# Pub/Sub publisher (singleton)
# =============================================================================


def _get_publisher():
    """Lazily initialise the Pub/Sub publisher (singleton)."""
    global _PUBLISHER, _PUBLISHER_INIT_ATTEMPTED
    if _PUBLISHER_INIT_ATTEMPTED:
        return _PUBLISHER
    _PUBLISHER_INIT_ATTEMPTED = True
    try:
        from google.cloud import pubsub_v1

        _PUBLISHER = pubsub_v1.PublisherClient()
    except Exception:
        logger.debug("Pub/Sub publisher unavailable (local/test env)")
    return _PUBLISHER


def _env_suffix() -> str:
    if os.environ.get("STAGING", "False") == "True":
        return "-staging"
    return ""


def _topic_path(billing_account_id: int):
    publisher = _get_publisher()
    if publisher is None:
        return None
    project_id = os.environ.get("GCP_PROJECT_ID", "saas-368716")
    topic_name = f"billing-account-{billing_account_id}{_env_suffix()}"
    return publisher.topic_path(project_id, topic_name)


def _ensure_topic(publisher, topic: str) -> None:
    """Create the Pub/Sub topic if it doesn't exist yet (cached per process)."""
    if topic in _known_topics:
        return
    try:
        publisher.create_topic(name=topic)
        logger.info("Created billing Pub/Sub topic: %s", topic)
    except Exception as exc:
        if hasattr(exc, "code") and callable(exc.code):
            from grpc import StatusCode

            if exc.code() == StatusCode.ALREADY_EXISTS:
                pass
            else:
                raise
        else:
            raise
    _known_topics.add(topic)


def _publish(
    billing_account_id: int,
    event_type: str,
    balance: float,
) -> None:
    publisher = _get_publisher()
    topic = _topic_path(billing_account_id)
    if publisher is None or topic is None:
        return

    payload = json.dumps(
        {
            "event_type": event_type,
            "billing_account_id": billing_account_id,
            "balance": balance,
        },
    ).encode("utf-8")

    try:
        _ensure_topic(publisher, topic)
        publisher.publish(topic, payload, thread="billing_event")
    except Exception:
        logger.warning(
            "Failed to publish billing event %s for account %s",
            event_type,
            billing_account_id,
            exc_info=True,
        )


# =============================================================================
# Session-level balance tracking
# =============================================================================


def track_balance_before(
    session,
    billing_account_id: int,
    balance: Union[float, Decimal],
) -> None:
    """Record the pre-mutation balance for a billing account.

    Call this *before* modifying ``BillingAccount.credits``.  Only the
    first call per ``(session, billing_account_id)`` is recorded — later
    calls are no-ops so that nested credit operations (e.g. deduct then
    auto-recharge) correctly compare the *original* balance with the
    *final* committed balance.
    """
    snapshots = session.info.setdefault(_SESSION_KEY, {})
    if billing_account_id not in snapshots:
        snapshots[billing_account_id] = {
            "previous": float(balance),
        }
        _ensure_after_commit_listener(session)


def track_balance_after(
    session,
    billing_account_id: int,
    balance: Union[float, Decimal],
) -> None:
    """Update the post-mutation balance for a billing account.

    Call this *after* modifying ``BillingAccount.credits``.  Each call
    overwrites the previous value so the listener always sees the final
    committed balance.
    """
    snapshots = session.info.get(_SESSION_KEY)
    if snapshots is None or billing_account_id not in snapshots:
        # track_balance_before wasn't called — record both
        snapshots = session.info.setdefault(_SESSION_KEY, {})
        snapshots[billing_account_id] = {
            "previous": float(balance),
            "final": float(balance),
        }
        _ensure_after_commit_listener(session)
        return

    snapshots[billing_account_id]["final"] = float(balance)


def _ensure_after_commit_listener(session) -> None:
    """Register a one-time ``after_commit`` listener on *this* session."""
    if session.info.get(_LISTENER_KEY):
        return
    session.info[_LISTENER_KEY] = True

    @sa_event.listens_for(session, "after_commit")
    def _on_commit(session):
        _flush_billing_events(session)


def _flush_billing_events(session) -> None:
    """Publish events for any balance transitions that crossed zero."""
    snapshots = session.info.pop(_SESSION_KEY, {})
    session.info.pop(_LISTENER_KEY, None)

    for ba_id, data in snapshots.items():
        prev = data["previous"]
        final = data.get("final", prev)
        if prev > 0 and final <= 0:
            _publish(ba_id, "credits_exhausted", final)
        elif prev <= 0 and final > 0:
            _publish(ba_id, "credits_restored", final)


# =============================================================================
# Legacy helpers (kept for backwards compatibility / direct use)
# =============================================================================


def publish_if_credits_exhausted(
    billing_account_id: int,
    previous_balance: Union[float, Decimal],
    new_balance: Union[float, Decimal],
    entity_type: str = "user",
    entity_id: str = "",
) -> None:
    """Publish ``credits_exhausted`` if balance just crossed from positive to non-positive."""
    prev = float(previous_balance)
    curr = float(new_balance)
    if prev > 0 and curr <= 0:
        _publish(billing_account_id, "credits_exhausted", curr)


def publish_if_credits_restored(
    billing_account_id: int,
    previous_balance: Union[float, Decimal],
    new_balance: Union[float, Decimal],
    entity_type: str = "user",
    entity_id: str = "",
) -> None:
    """Publish ``credits_restored`` if balance just crossed from non-positive to positive."""
    prev = float(previous_balance)
    curr = float(new_balance)
    if prev <= 0 and curr > 0:
        _publish(billing_account_id, "credits_restored", curr)
