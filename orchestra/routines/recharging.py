import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.settings import settings
from orchestra.web.api.admin.schema import RechargeModelRequest
from orchestra.web.api.admin.views import create_recharge_model, get_all_users_models

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


def recharge_credits():  # noqa: D103, WPS210
    engine = create_engine(str(settings.db_url))
    session_factory = sessionmaker(engine, expire_on_commit=False)
    with session_factory() as session:
        users_dao = UsersDAO(session)
        recharge_dao = RechargeDAO(session)

        all_users = get_all_users_models(users_dao)
        recharge_quantity = 2.5  # TODO: add this to the user table
        max_recharge = 10.0  # TODO: add this to the user table
        for user in all_users:
            recharge_qty = min(recharge_quantity, max_recharge - float(user.credits))

            if user.credits >= max_recharge or recharge_qty <= 0:
                continue

            recharge_obj = RechargeModelRequest(
                user_id=user.id,
                quantity=recharge_qty,
                type="free",
            )
            create_recharge_model(
                new_recharge_object=recharge_obj,
                recharge_dao=recharge_dao,
                user_dao=users_dao,
            )
            session.commit()
        logger.info(
            f"Recharged all users with {recharge_quantity} credits",
        )


if __name__ == "__main__":
    logger.info("Recharging user credits...")
    recharge_credits()
