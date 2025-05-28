from typing import List, Optional

from sqlalchemy import (
    select,
)
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import AuthUser

ASSISTANT_HIRING_APPROVAL_STATUSES = [
    None,
    "pending",
    "approved",
    "rejected",
    "revoked",
]


class AuthUserDAO:
    def __init__(self, session: Session):
        self.session = session

    def create(  # noqa: WPS211
        self,
        email: str,
        name: Optional[str] = None,
        last_name: Optional[str] = None,
        job_title: Optional[str] = None,
        image: Optional[str] = None,
    ) -> None:
        self.session.add(
            AuthUser(
                email=email,
                name=name,
                last_name=last_name,
                job_title=job_title,
                image=image,
            ),
        )

    def filter(
        self,
        id: Optional[str] = None,
        email: Optional[str] = None,
        assistant_hiring_approval: Optional[
            str
        ] = "__use_default_no_filter__",  # Sentinel
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[AuthUser]:  # Technically List[RowProxy]
        query = select(AuthUser)
        if id:
            query = query.where(AuthUser.id == id)
        if email:
            query = query.where(AuthUser.email == email)
        if assistant_hiring_approval != "__use_default_no_filter__":
            if assistant_hiring_approval is None:
                query = query.where(AuthUser.assistant_hiring_approval.is_(None))
            else:
                if assistant_hiring_approval not in ASSISTANT_HIRING_APPROVAL_STATUSES:
                    raise ValueError(
                        f"Invalid assistant hiring approval status for filtering: {assistant_hiring_approval}"
                    )
                query = query.where(
                    AuthUser.assistant_hiring_approval == assistant_hiring_approval
                )

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        rows = self.session.execute(query)
        return rows.fetchall()  # Returns List[RowProxy]

    def get_by_id(
        self, user_id: str
    ) -> Optional[AuthUser]:  # Technically Optional[RowProxy]
        """Return a single AuthUser object or None given a user_id."""
        found = self.filter(id=user_id)
        return found[0] if found else None

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = None,
        last_name: Optional[str] = None,
        job_title: Optional[str] = None,
        image: Optional[str] = None,
        tier: Optional[str] = None,
        queries_enabled: Optional[bool] = None,
        evaluations_enabled: Optional[bool] = None,
        has_claimed_approval_link: Optional[bool] = None,
        assistant_hiring_approval: Optional[str] = None,
    ) -> None:
        query = select(AuthUser)
        query = query.where(AuthUser.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if last_name:
                setattr(entry, "last_name", last_name)
            if job_title:
                setattr(entry, "job_title", job_title)
            if image:
                setattr(entry, "image", image)
            if tier:
                setattr(entry, "tier", tier)
            if queries_enabled is not None:
                setattr(entry, "queries_enabled", queries_enabled)
            if evaluations_enabled is not None:
                setattr(entry, "evaluations_enabled", evaluations_enabled)
            if assistant_hiring_approval is not None:
                if assistant_hiring_approval not in ASSISTANT_HIRING_APPROVAL_STATUSES:
                    raise ValueError(
                        f"Unsupported hiring approval status: {assistant_hiring_approval}"
                    )
                setattr(entry, "assistant_hiring_approval", assistant_hiring_approval)
            if has_claimed_approval_link is not None:
                setattr(entry, "has_claimed_approval_link", has_claimed_approval_link)

            self.session.commit()

    def delete(self, id: str):
        try:
            auth_user = self.session.query(AuthUser).filter_by(id=id).one()
            self.session.delete(auth_user)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    # -- Handle assistant hiring approval --
    def set_assistant_hiring_approval(
        self, user_id: str, status: Optional[str]
    ) -> bool:
        """Sets the assistant hiring approval status for a user."""
        if status not in ASSISTANT_HIRING_APPROVAL_STATUSES:
            raise ValueError(f"Invalid assistant hiring approval status: {status}")

        user_row = self.get_by_id(user_id)
        if user_row:
            auth_user_instance = user_row[0]
            auth_user_instance.assistant_hiring_approval = status
            return True
        return False

    def get_assistant_hiring_approval(self, user_id: str) -> Optional[str]:
        """Gets the assistant hiring approval status for a user."""
        user_row = self.get_by_id(user_id)
        if user_row:
            auth_user_instance = user_row[0]
            return auth_user_instance.assistant_hiring_approval
        return None

    def get_users_by_assistant_hiring_approval(
        self, status: str, limit: Optional[int] = None, offset: Optional[int] = None
    ) -> List[AuthUser]:
        """Returns users matching a specific hiring status (e.g., "pending")."""
        if status not in ASSISTANT_HIRING_APPROVAL_STATUSES or status is None:
            raise ValueError(
                "Unsupported or invalid asssistant hiring approval status for querying list."
            )
        query = select(AuthUser).where(AuthUser.assistant_hiring_approval == status)

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        return list(self.session.execute(query).scalars().all())
