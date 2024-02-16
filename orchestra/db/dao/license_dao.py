from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import License


class LicenseDAO:
    """Class for accessing license table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_license(
        self,
        name: str,
        image_url: str,
        description: str,
    ) -> None:
        """
        Add single license to session.

        :param name: name of a license.
        :param image_url: image_url of a license.
        :param description: description of a license.
        """
        self.session.add(
            License(
                name=name,
                image_url=image_url,
                description=description,
            ),
        )

    def get_all_licenses(self, limit: int, offset: int) -> List[License]:
        """
        Get all license models with limit/offset pagination.

        :param limit: limit of licenses.
        :param offset: offset of licenses.
        :return: stream of licenses.
        """
        raw_licenses = self.session.execute(
            select(License).limit(limit).offset(offset),
        )

        return list(raw_licenses.scalars().fetchall())

    def filter(
        self,
        name: Optional[str] = None,
    ) -> List[License]:
        """
        Get specific license model.

        :param name: name of license instance.
        :return: license models.
        """
        query = select(License)
        if name:
            query = query.where(License.name == name)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
