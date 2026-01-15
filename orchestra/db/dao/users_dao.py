import decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.utils.http_responses import not_found

# Constants for billing requirements
MIN_SPEND_FOR_MONTHLY_BILLING = 100.0  # $100 in credits (100 credits)
MIN_AUTORECHARGE_AMOUNT = 25.0  # $25 in credits (25 credits)


class UsersDAO:
    """Class for accessing users table."""

    def __init__(self, session: Session):
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
                autorecharge_threshold=0,
                autorecharge_qty=25,
                store_prompts=True,
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
            raise not_found("User ID")

    def get_user_by_stripe_id(self, stripe_id: str) -> Optional[Users]:
        """
        Get a user by their Stripe customer ID.

        :param stripe_id: The Stripe customer ID.
        :return: A Users object or None if not found.
        """
        query = select(Users).where(Users.stripe_customer_id == stripe_id)
        result = self.session.execute(query).scalars().first()
        return result

    def get_total_spending(self, user_id: str) -> float:
        """
        Calculate total spending for a user from the Query table.

        :param user_id: id of the user
        :return: total spending in credits (equivalent to dollars since 1 credit = $1)
        """
        # result = self.session.execute(
        #     select(func.sum(Query.credits)).where(
        #         Query.user_id == user_id,
        #     ),  # Count all queries since providers charge for failed requests too
        # ).scalar()

        # return float(result) if result else 0.0

        # ToDo: Replace with new credit deduction system when in place.
        return 0.0

    def can_enable_monthly_billing(self, user_id: str) -> bool:
        """
        Check if user has spent enough to enable monthly billing.

        :param user_id: id of the user
        :return: True if user has spent at least $100
        """
        total_spending = self.get_total_spending(user_id)
        return total_spending >= MIN_SPEND_FOR_MONTHLY_BILLING

    def is_telemetry_activated(self, id: str) -> bool:
        try:
            telemetry_activated = self.filter(id=id)[0].store_prompts
            return telemetry_activated if telemetry_activated is not None else True
        except IndexError:
            raise not_found("User ID")

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
            new_credits = user.credits + decimal.Decimal(quantity)
            setattr(  # noqa: B010
                user,
                "credits",
                new_credits,
            )

    def set_prompt_telemetry(
        self,
        user_id: str,
        activated: bool,
    ) -> None:
        """
        Update the active status of prompt telemetry.

        :param user_id: id of a user.
        :param activated: whether to store or not the prompts.
        """
        query = select(Users)
        query = query.where(Users.id == user_id)

        raw_users = self.session.execute(query)
        user = raw_users.scalars().first()
        if user is not None:
            setattr(user, "store_prompts", activated)

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

        Validates that user has spent at least $100 before enabling monthly billing.
        No grandfathering - all users must meet spending requirements.

        :param user_id: id of a user.
        :param enable: whether to enable (true) or disable (false) autorecharge.
        :raises ValueError: if trying to enable autorecharge without sufficient spending
        """
        # First check if user exists - this will raise HTTPException if not found
        user = self.get_user_with_id(user_id)

        # Check spending requirements when trying to ENABLE autorecharge
        if enable and not self.can_enable_monthly_billing(user_id):
            total_spending = self.get_total_spending(user_id)
            raise ValueError(
                f"User must spend at least ${MIN_SPEND_FOR_MONTHLY_BILLING:.2f} before enabling monthly billing. "
                f"Current spending: ${total_spending:.2f}",
            )

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
                user,
                "autorecharge_threshold",
                decimal.Decimal(threshold),
            )  # noqa: B010

    def set_autorecharge_qty(
        self,
        user_id: str,
        qty: float,
    ) -> None:
        """
        Modify the autorecharge quantity for a user.

        Validates that the quantity meets the minimum requirement.

        :param user_id: id of a user.
        :param qty: quantity of credits to be added during autorecharge.
        :raises ValueError: if quantity is below minimum requirement
        """
        if qty < MIN_AUTORECHARGE_AMOUNT:
            raise ValueError(
                f"Minimum auto-recharge amount is ${MIN_AUTORECHARGE_AMOUNT:.2f} "
                f"({MIN_AUTORECHARGE_AMOUNT:.0f} credits). Provided: ${qty:.2f}",
            )

        user = self.get_user_with_id(user_id)
        if user is not None:
            setattr(user, "autorecharge_qty", decimal.Decimal(qty))  # noqa: B010

    def set_frozen_status(
        self,
        user_id: str,
        frozen: bool,
    ) -> None:
        """
        Set the frozen status of a user account.

        :param user_id: id of a user.
        :param frozen: whether the account should be frozen (true) or unfrozen (false).
        """
        user = self.get_user_with_id(user_id)
        if user is not None:
            setattr(user, "frozen", frozen)
            self.session.commit()

    def is_account_frozen(self, user_id: str) -> bool:
        """
        Check if a user account is frozen.

        :param user_id: id of a user.
        :return: True if the account is frozen, False otherwise.
        """
        try:
            user = self.get_user_with_id(user_id)
            return user.frozen
        except Exception as e:
            self.session.rollback()
            return False
