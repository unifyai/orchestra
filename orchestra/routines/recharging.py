import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from orchestra.consts import RECHARGE_TYPE_AUTO
from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.db.models.orchestra_models import Users as User
from orchestra.pricing import credits_to_usd
from orchestra.settings import settings
from orchestra.web.api.admin.schema import RechargeModelRequest
from orchestra.web.api.admin.views import create_recharge_model, get_all_users_models

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


def recharge_credits(worker_id=None, amount=2.5):  # noqa: D103, WPS210
    url = str(settings.db_url)
    if worker_id:
        url = url.replace("orchestra_test", f"orchestra_test_{worker_id}")
    engine = create_engine(url)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    with session_factory() as session:
        all_users = get_all_users_models(session=session)
        recharge_quantity = amount  # TODO: add this to the user table
        for user in all_users:

            recharge_obj = RechargeModelRequest(
                user_id=user.id,
                quantity=recharge_quantity,
                type="free",
            )
            create_recharge_model(
                new_recharge_object=recharge_obj,
                session=session,
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
        ts = datetime.now(UTC)

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
