import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Query, QueryTagAssociation, Tag


class QueryDAO:
    """Class for accessing query table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_query(
        self,
        user_id: str,
        at: datetime.datetime,
        model_provider_str: str,
        endpoint_id: Optional[int],
        custom_endpoint_id: Optional[int],
        local_endpoint_id: Optional[int],
        credits: float,
        query_body: str,
        response_body: str,
        status_code: int,
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
            model_provider_str=model_provider_str,
            endpoint_id=endpoint_id,
            custom_endpoint_id=custom_endpoint_id,
            local_endpoint_id=local_endpoint_id,
            credits=credits,
            query_body=query_body,
            response_body=response_body,
            signature=signature,
            used_router=used_router,
            router=router,
            status_code=status_code,
        )
        self.session.add(new_query)

        # adds tags & avoids race conditions
        if tags:
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
                    user_id=user_id,
                    query_id=new_query.id,
                    tag_id=tag.id,
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
        start_time: Optional[datetime.datetime] = None,
        end_time: Optional[datetime.datetime] = None,
        endpoint_ids: Optional[list[int]] = None,
        custom_endpoint_ids: Optional[list[int]] = None,
        local_endpoint_ids: Optional[list[int]] = None,
        credits: Optional[float] = None,
        signature: Optional[str] = None,
        used_router: Optional[str] = None,
        router: Optional[str] = None,
        tags: Optional[list[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        status_code: Optional[int] = None,
    ) -> List[Query]:
        query = select(Query)
        if user_id:
            query = query.where(Query.user_id == user_id)

        if start_time:
            query = query.filter(Query.at > start_time)
        if end_time:
            query = query.filter(Query.at < end_time)
        query.order_by(Query.at.desc())

        endpoint_filters = []
        if endpoint_ids:
            endpoint_filters.append(Query.endpoint_id.in_(endpoint_ids))
        if custom_endpoint_ids:
            endpoint_filters.append(Query.custom_endpoint_id.in_(custom_endpoint_ids))
        if local_endpoint_ids:
            endpoint_filters.append(Query.local_endpoint_id.in_(local_endpoint_ids))

        if endpoint_filters:
            query = query.where(or_(*endpoint_filters))

        if credits:
            query = query.where(Query.credits == credits)
        if signature:
            query = query.where(Query.signature == signature)
        if used_router:
            query = query.where(Query.used_router == used_router)
        if router:
            query = query.where(Query.router == router)
        if status_code:
            query = query.where(Query.status_code == status_code)
        if tags:
            tag_alias = aliased(Tag)
            query = query.join(
                QueryTagAssociation,
                Query.id == QueryTagAssociation.query_id,
            )
            query = query.join(tag_alias, QueryTagAssociation.tag_id == tag_alias.id)
            tag_filters = [tag_alias.tag_name == tag for tag in tags]
            query = query.where(and_(*tag_filters))

        if limit:
            query = query.limit(limit)
        if offset:
            query = query.limit(offset)

        raw_queries = self.session.execute(query)

        results = list(raw_queries.scalars().fetchall())
        ret = []
        for q in results:
            ret.append(
                {
                    "endpoint": q.model_provider_str,
                    "query_body": q.query_body,
                    "response_body": q.response_body,
                    "at": q.at,
                    "credits": q.credits,
                    "status_code": q.status_code,
                },
            )
        return ret
