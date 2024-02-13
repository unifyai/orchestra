import asyncio
import logging
import os

from sqlalchemy.orm import sessionmaker, Session, create_engine

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.admin.schema import RechargeModelRequest
from orchestra.web.api.admin.views import create_recharge_model, get_all_users_models

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


async def put_data_to_db(  # noqa: D103, WPS211, WPS210 # TODO: Understand this
    async_session,
):
    async with async_session() as session:
        users_dao = UsersDAO(session)
        recharge_dao = RechargeDAO(session)

        all_users = get_all_users_models(users_dao)
        recharge_quantity = 2.5
        max_recharge_grant = 10.0
        for user in all_users:
            if user.credits >= max_recharge_grant:
                continue
            recharge_obj = RechargeModelRequest(
                user_id=user.id,
                quantity=recharge_quantity,
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


async def recharge_credits():  # noqa: D103, WPS210
    user = os.getenv("ORCHESTRA_DB_USER", "orchestra")
    password = os.getenv("ORCHESTRA_DB_PASS", "orchestra")
    host = os.getenv("ORCHESTRA_DB_HOST", "localhost")
    port = os.getenv("ORCHESTRA_DB_PORT", "5432")
    db_name = os.getenv("ORCHESTRA_DB_BASE", "orchestra")
    db_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}"  # noqa: WPS221, E501
    logger.info(db_url)
    engine = create_engine(db_url)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=Session)
    put_data_to_db(async_session)


if __name__ == "__main__":
    logger.info("Recharging user credits...")
    asyncio.run(recharge_credits())
