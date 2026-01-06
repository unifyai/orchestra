"""Async version of beta_list_dao for use with AsyncSession."""

from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import BetaList


class AsyncBetaListDAO:
    """Class for accessing beta list table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_beta_list(self, email: str, type: str) -> None:
        self.session.add(BetaList(email=email, type=type))
