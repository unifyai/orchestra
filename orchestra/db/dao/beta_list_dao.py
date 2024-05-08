from fastapi import Depends
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import BetaList


class BetaListDAO:
    """Class for accessing beta list table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_beta_list(self, email: str, type: str) -> None:
        self.session.add(BetaList(email=email, type=type))
