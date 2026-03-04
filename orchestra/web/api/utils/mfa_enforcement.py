"""Server-side MFA enforcement for org members."""

import logging

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.mfa_credential_dao import MFACredentialDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Organization, OrganizationMember

logger = logging.getLogger(__name__)


def check_org_mfa_enforcement():
    def dependency(
        request: Request,
        session: Session = Depends(get_db_session),
    ) -> None:
        user_id = getattr(request.state, "user_id", None)
        if not user_id:
            return

        memberships = (
            session.query(OrganizationMember)
            .join(Organization, OrganizationMember.organization_id == Organization.id)
            .filter(
                OrganizationMember.user_id == user_id,
                Organization.require_mfa.is_(True),
            )
            .all()
        )

        if not memberships:
            return

        mfa_dao = MFACredentialDAO(session)
        if not mfa_dao.has_enabled_mfa(user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "mfa_required",
                    "message": "Your organization requires two-factor authentication. Please set up MFA first.",
                },
            )

    return dependency
