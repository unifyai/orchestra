from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Tag


class TagDAO:
    """Class for accessing tag table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def get_all_tags(self, user_id):
        query = select(Tag)
        query = query.where(Tag.user_id == user_id)
        rows = self.session.execute(query)
        tag_data = list(rows.scalars().fetchall())
        return [t.tag_name for t in tag_data]
