# TODO: Limit + increase has to be stored in the user
import math

from orchestra.db.dao.users_dao import UsersDAO
from orchestra.routines.recharging import recharge_credits
from orchestra.web.api.admin.views import get_user


def test_recharge(dbsession) -> None:
    """Tests the recharge routine code."""
    recharge_credits()
    users_dao = UsersDAO(dbsession)
    # user has (current + recharge ) < limit
    simple = get_user("recharge_simple", users_dao)[0]
    assert math.isclose(simple.credits, 3.5)
    # user has (current + recharge ) > limit
    recharge_limited = get_user("recharge_limited", users_dao)[0]
    assert math.isclose(recharge_limited.credits, 10)
    # user has current == limit
    recharge_not_needed_a = get_user("recharge_not_needed_a", users_dao)[0]
    assert recharge_not_needed_a.credits == 10
    # user has current > limit
    recharge_not_needed_b = get_user("recharge_not_needed_b", users_dao)[0]
    assert recharge_not_needed_b.credits == 20
