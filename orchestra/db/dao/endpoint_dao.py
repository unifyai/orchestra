import datetime
from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Endpoint, Model, Provider


class EndpointDAO:
    """Class for accessing endpoint table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_endpoint(
        self,
        mdl_id: int,
        provider_id: int,
        created_at: datetime.datetime,
        active: bool,
    ) -> None:
        """
        Add single endpoint to session.

        :param mdl_id: mdl_id of a endpoint.
        :param provider_id: provider_id of a endpoint.
        :param created_at: created_at of a endpoint.
        :param active: is endpoint active.
        """
        self.session.add(
            Endpoint(
                mdl_id=mdl_id,
                provider_id=provider_id,
                created_at=created_at,
                active=active,
            ),
        )

    def get_all_endpoints_raw(self, limit: int, offset: int) -> List[Endpoint]:
        """
        Get all endpoint models with limit/offset pagination.

        :param limit: limit of endpoints.
        :param offset: offset of endpoints.
        :return: stream of endpoints.
        """
        raw_endpoints = self.session.execute(
            select(Endpoint).limit(limit).offset(offset),
        )

        return list(raw_endpoints.scalars().fetchall())

    def get_endpoints_of(
        self,
        models: Optional[Tuple[str, ...]] = None,
        only_from: Optional[Tuple[str, ...]] = None,
    ) -> List[str]:
        query = select(Endpoint, Model, Provider).join(Model).join(Provider)
        query = query.where(Model.active == True).where(Endpoint.active == True)
        if models and models[0] is not None:
            query = query.where(Model.mdl_code.in_(models))
        if only_from and only_from[0] is not None:
            query = query.where(Provider.name.in_(only_from))
        rows = self.session.execute(query)
        return list(rows.fetchall())

    def filter(
        self,
        id: Optional[int] = None,  # noqa: WPS125
        mdl_id: Optional[int] = None,
        provider_id: Optional[int] = None,
        created_at: Optional[datetime.datetime] = None,
        active: Optional[bool] = None,
    ) -> List[Endpoint]:
        """
        Get specific endpoint model.

        :param id: id of endpoint instance.
        :param mdl_id: mdl_id of endpoint instance.
        :param provider_id: provider_id of endpoint instance.
        :param created_at: created_at of endpoint instance.
        :param active: is model instance active.
        :return: endpoint models.
        """
        query = select(Endpoint)
        if id:
            query = query.where(Endpoint.id == id)
        if mdl_id:
            query = query.where(Endpoint.mdl_id == mdl_id)
        if provider_id:
            query = query.where(Endpoint.provider_id == provider_id)
        if created_at:
            query = query.where(Endpoint.created_at == created_at)
        if active:
            query = query.where(Endpoint.active == active)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
