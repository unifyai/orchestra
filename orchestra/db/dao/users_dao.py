import decimal
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.utils.http_responses import user_id_not_found


class UsersDAO:
    """Class for accessing users table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_users(
        self,
        id: str,  # noqa: WPS125
        credits: float,
    ) -> None:
        """
        Add single users to session.

        :param id: id of a users.
        :param credits: credits of a users.
        """
        self.session.add(
            Users(
                id=id,
                credits=credits,
                stripe_customer_id=None,
                autorecharge=False,
                autorecharge_threhsold=0,
                autorecharge_qty=0,
            ),
        )

    def get_all_users(self) -> List[Users]:
        """
        Get all users models with limit/offset pagination.

        :return: stream of users.
        """
        raw_users = self.session.execute(select(Users))
        return list(raw_users.scalars().fetchall())

    def filter(
        self,
        id: Optional[str],  # noqa: WPS125
    ) -> List[Users]:
        """
        Get specific users model.

        :param id: id of users instance.
        :return: stream of users.
        """
        query = select(Users)
        query = query.where(Users.id == id)

        raw_users = self.session.execute(query)

        return list(raw_users.scalars().fetchall())

    def get_user_with_id(self, id: str) -> Users:
        try:
            return self.filter(id=id)[0]
        except IndexError:
            raise user_id_not_found

    def recharge_credit(
        self,
        user_id: str,
        quantity: float,
    ) -> None:
        """
        Recharge credit of a users.

        :param user_id: id of a user.
        :param quantity: positive number of credits to recharge.
        """
        query = select(Users)
        query = query.where(Users.id == user_id)

        raw_users = self.session.execute(query)
        user = raw_users.scalars().first()
        if user is not None:
            setattr(  # noqa: B010
                user,
                "credits",
                user.credits + decimal.Decimal(quantity),
            )

    def set_stripe_customer_id(
        self,
        user_id: str,
        stripe_id: str,
    ) -> None:
        """
        Modify the stripe customer id of a user.

        :param user_id: id of a user.
        :param stripe_id: stripe customer id of a user.
        """
        user = self.get_user_with_id(user_id)
        if user is not None:
            setattr(user, "stripe_customer_id", stripe_id)  # noqa: B010

    def enable_autorecharge(
        self,
        user_id: str,
        enable: bool,
    ) -> None:
        """
        Enable/disable autorecharge for a user.

        :param user_id: id of a user.
        :param enable: whether to enable (true) or disable (false) autorecharge.
        """
        user = self.get_user_with_id(user_id)
        if user is not None:
            setattr(user, "autorecharge", enable)  # noqa: B010

    def set_autorecharge_threshold(
        self,
        user_id: str,
        threshold: float,
    ) -> None:
        """
        Modify the autorecharge threshold for a user.

        :param user_id: id of a user.
        :param threshold: limit quantity of credits before triggering autorecharge.
        """
        user = self.get_user_with_id(user_id)
        if user is not None:
            setattr(
                user, "autorecharge_threshold", decimal.Decimal(threshold)
            )  # noqa: B010

    def set_autorecharge_qty(
        self,
        user_id: str,
        qty: float,
    ) -> None:
        """
        Modify the autorecharge quantity for a user.

        :param user_id: id of a user.
        :param threshold: quantity of credits to be added during autorecharge.
        """
        user = self.get_user_with_id(user_id)
        if user is not None:
            setattr(user, "autorecharge_qty", decimal.Decimal(qty))  # noqa: B010
