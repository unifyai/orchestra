from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import BetaList


class BetaListDAO:
    """Class for accessing beta list table."""

    def __init__(self, session: Session):
        self.session = session

    def create_beta_list(self, email: str, type: str) -> None:
        self.session.add(BetaList(email=email, type=type))
