import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.lib.billing import credits_to_usd
from orchestra.lib.time import month_end_utc
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
            invoice_group = month_end_utc(at)

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
                invoice_group = month_end_utc(at)

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
