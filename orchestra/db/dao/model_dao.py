import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Model


class ModelDAO:
    """Class for accessing model table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_model(  # noqa: WPS211
        self,
        mdl_code: str,
        uploaded_at: datetime.datetime,
        active: bool,
    ) -> None:
        """
        Add single model to session.

        :param mdl_code: mdl_code of a model.
        :param uploaded_at: uploaded_at of a model.
        :param active: is model active.
        """
        self.session.add(
            Model(
                mdl_code=mdl_code,
                uploaded_at=uploaded_at,
                active=active,
            ),
        )

    def get_all_models(self, limit: int, offset: int) -> List[Model]:
        """
        Get all model models with limit/offset pagination.

        :param limit: limit of models.
        :param offset: offset of models.
        :return: stream of models.
        """
        raw_models = self.session.execute(
            select(Model).limit(limit).offset(offset),
        )

        return list(raw_models.scalars().fetchall())

    def get_active_models(self) -> List[str]:
        """
        Get active model models.

        :return: names of models.
        """
        raw_models = self.session.execute(
            select(Model.mdl_code).where(Model.active == True),
        )

        return list(raw_models.scalars().fetchall())

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        mdl_code: Optional[str] = None,
        uploaded_at: Optional[datetime.datetime] = None,
        active: Optional[bool] = None,
    ) -> List[Model]:
        """
        Get specific model model.

        :param id: id of model instance.
        :param mdl_code: mdl_code of model instance.
        :param uploaded_at: uploaded_at of model instance.
        :param active: is model instance active.
        :return: model models.
        """
        query = select(Model)
        if id:
            query = query.where(Model.id == id)
        if mdl_code:
            query = query.where(Model.mdl_code == mdl_code)
        if uploaded_at:
            query = query.where(Model.uploaded_at == uploaded_at)
        if active:
            query = query.where(Model.active == active)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update_model(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        mdl_code: Optional[str] = None,
        uploaded_at: Optional[datetime.datetime] = None,
        active: Optional[bool] = None,
    ) -> None:
        """
        Update specific model model.

        :param id: id of model instance.
        :param mdl_code: mdl_code of model instance.
        :param uploaded_at: uploaded_at of model instance.
        :param active: is model instance active.
        """
        query = select(Model)
        query = query.where(Model.id == id)
        raw_model = self.session.execute(query)
        model = raw_model.scalars().first()
        if model is not None:
            if mdl_code:
                setattr(model, "mdl_code", mdl_code)  # noqa: B010
            if uploaded_at:
                setattr(model, "uploaded_at", uploaded_at)  # noqa: B010
            if active is not None:
                setattr(model, "active", active)  # noqa: B010
