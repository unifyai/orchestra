import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
