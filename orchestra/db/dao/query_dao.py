import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from sqlalchemy import select, and_, func
from sqlalchemy.orm import aliased

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Query, Tag, QueryTagAssociation


class QueryDAO:
    """Class for accessing query table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_query(
        self,
        user_id: str,
        at: datetime.datetime,
        endpoint_id: int,
        credits: float,
        prompt: Optional[str] = None,
        signature: Optional[str] = None,
        used_router: Optional[bool] = None,
        router: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> None:
        """
        Add single query to session.

        :param user_id: user_id of a query.
        :param at: at of a query.
        :param endpoint_id: endpoint_id of a query.
        :param credits: credits of a query.
        """

        new_query = Query(
            user_id=user_id,
            at=at,
            endpoint_id=endpoint_id,
            credits=credits,
            prompt=prompt,
            signature=signature,
            used_router=used_router,
            router=router,
        )
        self.session.add(new_query)

        # handles tags
        for tag_name in tags:
            tag = (
                self.session.query(Tag)
                .filter_by(user_id=user_id, tag_name=tag_name)
                .first()
            )

            if not tag:
                tag = Tag(user_id=user_id, tag_name=tag_name)
                self.session.add(tag)
                self.session.flush()

            query_tag_association = QueryTagAssociation(
                user_id=user_id, query_id=new_query.id, tag_id=tag.id
            )

            try:
                self.session.add(query_tag_association)
                self.session.commit()
            except IntegrityError:
                self.session.rollback()

    def get_all_queries(self, limit: int, offset: int) -> List[Query]:
        """
        Get all query models with limit/offset pagination.

        :param limit: limit of queries.
        :param offset: offset of queries.
        :return: stream of queries.
        """
        raw_queries = self.session.execute(
            select(Query).limit(limit).offset(offset),
        )

        return list(raw_queries.scalars().fetchall())

    def filter(
        self,
        user_id: Optional[str] = None,
        at: Optional[datetime.datetime] = None,
        endpoint_id: Optional[int] = None,
        credits: Optional[float] = None,
        signature: Optional[str] = None,
        used_router: Optional[str] = None,
        router: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> List[Query]:
        """
        Get specific query model.

        :param user_id: user_id of query instance.
        :param at: at of query instance.
        :param endpoint_id: endpoint_id of query instance.
        :param credits: credits of query instance.
        :return: query instance.
        """
        query = select(Query)
        if user_id:
            query = query.where(Query.user_id == user_id)
        if at:
            query = query.where(Query.at == at)
        if endpoint_id:
            query = query.where(Query.endpoint_id == endpoint_id)
        if credits:
            query = query.where(Query.credits == credits)
        if signature:
            query = query.where(Query.signature == signature)
        if used_router:
            query = query.where(Query.used_router == used_router)
        if router:
            query = query.where(Query.router == router)
        
        if tags:
            tag_alias = aliased(Tag)
            query = query.join(QueryTagAssociation, Query.id == QueryTagAssociation.query_id)
            query = query.join(tag_alias, QueryTagAssociation.tag_id == tag_alias.id)
            tag_filters = [tag_alias.tag_name == tag for tag in tags]
            query = query.where(and_(*tag_filters))


        raw_queries = self.session.execute(query)

        return list(raw_queries.scalars().fetchall())
