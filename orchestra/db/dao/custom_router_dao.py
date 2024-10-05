from typing import List

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CustomRouter


class CustomRouterDAO:
    """Class for accessing custom router table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_custom_router(
        self,
        user_id: str,
        router_name: str,
        router_id: str,
    ) -> None:
        self.session.add(
            CustomRouter(user_id=user_id, router_name=router_name, router_id=router_id),
        )

    def get_router_id(self, user_id: str, router_name) -> List[CustomRouter]:
        query = (
            select(CustomRouter)
            .where(CustomRouter.router_name == router_name)
            .where((CustomRouter.user_id == user_id) | (CustomRouter.user_id == None))
        )

        raw_custom_routers = self.session.execute(query)
        return list(raw_custom_routers.scalars().fetchall())
