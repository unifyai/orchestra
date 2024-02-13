from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Provider


class ProviderDAO:
    """Class for accessing provider table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_provider(
        self,
        name: str,
        image_url: str,
        description: str,
    ) -> None:
        """
        Add single provider to session.

        :param name: name of a provider.
        :param image_url: image_url of a provider.
        :param description: description of a provider.
        """
        self.session.add(
            Provider(
                name=name,
                image_url=image_url,
                description=description,
            ),
        )

    def get_all_providers(self, limit: int, offset: int) -> List[Provider]:
        """
        Get all provider models with limit/offset pagination.

        :param limit: limit of providers.
        :param offset: offset of providers.
        :return: stream of providers.
        """
        raw_providers = self.session.execute(
            select(Provider).limit(limit).offset(offset),
        )

        return list(raw_providers.scalars().fetchall())

    def filter(
        self,
        id: Optional[int] = None,  # noqa: WPS125
        name: Optional[str] = None,
    ) -> List[Provider]:
        """
        Get specific provider model.

        :param id: id of provider instance.
        :param name: name of provider instance.
        :return: provider models.
        """
        query = select(Provider)
        if id:
            query = query.where(Provider.id == id)
        if name:
            query = query.where(Provider.name == name)
        raw_providers = self.session.execute(query)
        return list(raw_providers.scalars().fetchall())
