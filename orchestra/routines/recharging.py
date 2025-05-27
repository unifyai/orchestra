import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from orchestra.consts import RECHARGE_TYPE_AUTO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.db.models.orchestra_models import Users as User
from orchestra.pricing import credits_to_usd
from orchestra.settings import settings
from orchestra.web.api.admin.views import get_all_users_models

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


def recharge_credits(worker_id=None, amount=2.5, session=None):  # noqa: D103, WPS210
    """Recharge all users with the specified amount of credits."""
    if session is not None:
        # Use the provided session
        all_users = get_all_users_models(session=session)
        recharge_quantity = amount  # TODO: add this to the user table
        for user in all_users:
            # Directly handle the recharge logic instead of calling the FastAPI endpoint
            user_dao = UsersDAO(session)
            recharge_dao = RechargeDAO(session)

            # Recharge the user's credits
            user_dao.recharge_credit(
                user_id=user.id,
                quantity=recharge_quantity,
            )

            # Create the recharge record
            at = datetime.now(timezone.utc)
            amount_usd = credits_to_usd(int(recharge_quantity))
            # Use month-end date for invoice grouping
            first_next_month = (at.replace(day=1) + timedelta(days=32)).replace(day=1)
            invoice_group = (first_next_month - timedelta(microseconds=1)).date()

            recharge_dao.create_recharge(
                user_id=user.id,
                quantity=int(recharge_quantity),
                amount_usd=amount_usd,
                invoice_group=invoice_group,
                type_="free",
                transaction_id=None,
            )

        # Don't commit when using external session - let the caller handle it
        logger.info(
            f"Recharged all users with {recharge_quantity} credits",
        )
    else:
        # Create our own session (original behavior)
        url = str(settings.db_url)
        if worker_id:
            url = url.replace("orchestra_test", f"orchestra_test_{worker_id}")
        engine = create_engine(url)
        session_factory = sessionmaker(engine, expire_on_commit=False)
        with session_factory() as session:
            all_users = get_all_users_models(session=session)
            recharge_quantity = amount  # TODO: add this to the user table
            for user in all_users:
                # Directly handle the recharge logic instead of calling the FastAPI endpoint
                user_dao = UsersDAO(session)
                recharge_dao = RechargeDAO(session)

                # Recharge the user's credits
                user_dao.recharge_credit(
                    user_id=user.id,
                    quantity=recharge_quantity,
                )

                # Create the recharge record
                at = datetime.now(timezone.utc)
                amount_usd = credits_to_usd(int(recharge_quantity))
                # Use month-end date for invoice grouping
                first_next_month = (at.replace(day=1) + timedelta(days=32)).replace(
                    day=1,
                )
                invoice_group = (first_next_month - timedelta(microseconds=1)).date()

                recharge_dao.create_recharge(
                    user_id=user.id,
                    quantity=int(recharge_quantity),
                    amount_usd=amount_usd,
                    invoice_group=invoice_group,
                    type_="free",
                    transaction_id=None,
                )

            session.commit()
            logger.info(
                f"Recharged all users with {recharge_quantity} credits",
            )


if __name__ == "__main__":
    logger.info("Recharging user credits...")
    recharge_credits()

# helper ----------------------------------------------------------------------
def _month_end_utc(ts: datetime | None = None) -> datetime:
    """Return the 23:59:59.999 of the month that `ts` falls in (UTC)."""
    if ts is None:
        ts = datetime.now(timezone.utc)

    first_next_month = (ts.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_next_month - timedelta(microseconds=1)


def _queue_recharge(session: Session, user: User, credits: int) -> None:
    """
    NEW behaviour – just record a recharge row.

    Stripe is invoiced in bulk by the monthly-invoicing cron so we
    do **not** call Stripe here.
    """
    session.add(
        Recharge(
            user_id=user.id,
            type=RECHARGE_TYPE_AUTO,
            quantity=Decimal(credits),
            amount_usd=credits_to_usd(credits),
            invoice_group=_month_end_utc(),
            status=RechargeStatus.PENDING_INVOICE,
        ),
    )
