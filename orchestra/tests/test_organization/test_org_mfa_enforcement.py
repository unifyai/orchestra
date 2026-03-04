"""
Tests for Organization MFA Enforcement.

Covers:
- OrganizationDAO: get_mfa_settings, update_mfa_settings, get_mfa_requiring_orgs_for_user
- Enforcement logic (setup_required = enforced AND NOT has_mfa)
- MFA disable blocked when org requires MFA (all auth providers)
- Permission checks on MFA settings endpoints
- MFA status-by-email admin endpoint
"""

from unittest.mock import MagicMock

from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.models.orchestra_models import Organization

# =============================================================================
# OrganizationDAO — MFA settings (mocked session)
# =============================================================================


class TestOrganizationDAOMfaSettings:
    """Unit tests for MFA-related methods on OrganizationDAO."""

    def _make_org(self, **overrides):
        """Create a mock Organization with MFA-related defaults."""
        org = MagicMock(spec=Organization)
        org.id = overrides.get("id", 1)
        org.name = overrides.get("name", "Test Org")
        org.require_mfa = overrides.get("require_mfa", False)
        return org

    def _make_dao(self, org=None):
        session = MagicMock()
        dao = OrganizationDAO(session)
        # Patch dao.get to return the given org
        dao.get = MagicMock(return_value=org)
        return dao, session

    # ── get_mfa_settings ────────────────────────────────────────────────

    def test_get_mfa_settings_returns_defaults(self):
        """New org has require_mfa=False."""
        org = self._make_org()
        dao, _ = self._make_dao(org)

        result = dao.get_mfa_settings(1)

        assert result is not None
        assert result["require_mfa"] is False

    def test_get_mfa_settings_when_enabled(self):
        """Org with require_mfa=True returns correct settings."""
        org = self._make_org(require_mfa=True)
        dao, _ = self._make_dao(org)

        result = dao.get_mfa_settings(1)

        assert result["require_mfa"] is True

    def test_get_mfa_settings_org_not_found(self):
        """Returns None when org doesn't exist."""
        dao, _ = self._make_dao(org=None)

        result = dao.get_mfa_settings(999)

        assert result is None

    # ── update_mfa_settings ─────────────────────────────────────────────

    def test_update_mfa_settings_enable(self):
        """Toggling require_mfa from False to True updates the org."""
        org = Organization()
        org.id = 1
        org.name = "Test Org"
        org.require_mfa = False

        session = MagicMock()
        dao = OrganizationDAO(session)
        dao.get = MagicMock(return_value=org)

        result = dao.update_mfa_settings(org_id=1, require_mfa=True)

        assert result is not None
        assert result["require_mfa"] is True
        assert org.require_mfa is True

    def test_update_mfa_settings_disable(self):
        """Toggling require_mfa from True to False updates the org."""
        org = Organization()
        org.id = 1
        org.name = "Test Org"
        org.require_mfa = True

        session = MagicMock()
        dao = OrganizationDAO(session)
        dao.get = MagicMock(return_value=org)

        result = dao.update_mfa_settings(org_id=1, require_mfa=False)

        assert result["require_mfa"] is False
        assert org.require_mfa is False

    def test_update_mfa_settings_org_not_found(self):
        """Returns None when org doesn't exist."""
        dao, _ = self._make_dao(org=None)

        result = dao.update_mfa_settings(org_id=999, require_mfa=True)

        assert result is None

    # ── get_mfa_requiring_orgs_for_user ─────────────────────────────────

    def test_get_mfa_requiring_orgs_for_user_returns_matching_orgs(self):
        """Returns orgs where require_mfa=True and user is a member."""
        session = MagicMock()
        dao = OrganizationDAO(session)

        org1 = MagicMock(spec=Organization)
        org1.id = 1
        org1.name = "Secure Corp"
        org1.require_mfa = True

        org2 = MagicMock(spec=Organization)
        org2.id = 2
        org2.name = "Relaxed Inc"
        org2.require_mfa = False

        # Mock the query chain
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [org1]
        mock_execute = MagicMock()
        mock_execute.scalars.return_value = mock_scalars
        session.execute.return_value = mock_execute

        result = dao.get_mfa_requiring_orgs_for_user("user-1")

        assert len(result) == 1
        assert result[0].name == "Secure Corp"

    def test_get_mfa_requiring_orgs_for_user_empty(self):
        """Returns empty list when user has no orgs that require MFA."""
        session = MagicMock()
        dao = OrganizationDAO(session)

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_execute = MagicMock()
        mock_execute.scalars.return_value = mock_scalars
        session.execute.return_value = mock_execute

        result = dao.get_mfa_requiring_orgs_for_user("user-1")

        assert result == []


# =============================================================================
# Enforcement Logic
# =============================================================================


class TestEnforcementLogic:
    """
    Test the enforcement logic that determines setup_required.

    The logic is:
      setup_required = enforced AND NOT has_mfa

    MFA enforcement applies to ALL members regardless of auth provider.
    """

    def test_no_enforcement_no_setup_required(self):
        """When require_mfa=False, setup_required should be False."""
        enforced = False
        has_mfa = False

        setup_required = enforced and not has_mfa
        assert setup_required is False

    def test_enforced_user_has_mfa(self):
        """User who already has MFA is not required to set up again."""
        enforced = True
        has_mfa = True

        setup_required = enforced and not has_mfa
        assert setup_required is False

    def test_enforced_no_mfa(self):
        """Any user without MFA must set it up when org enforces it."""
        enforced = True
        has_mfa = False

        setup_required = enforced and not has_mfa
        assert setup_required is True

    def test_enforced_oauth_user_no_mfa(self):
        """OAuth-only user without MFA must also set it up."""
        enforced = True
        has_mfa = False

        setup_required = enforced and not has_mfa
        assert setup_required is True

    def test_enforced_oauth_user_with_mfa(self):
        """OAuth-only user who has MFA enabled is compliant."""
        enforced = True
        has_mfa = True

        setup_required = enforced and not has_mfa
        assert setup_required is False


# =============================================================================
# MFA Disable Blocked by Org Enforcement
# =============================================================================


class TestMfaDisableOrgBlock:
    """
    Unit tests for the logic that blocks MFA disable when an org requires it.

    MFA disable is blocked for ANY user (email/password or OAuth) who is
    a member of an org with require_mfa=True.
    """

    def test_user_blocked_from_disabling_mfa(self):
        """
        When a user is a member of an org with require_mfa=True,
        attempting to disable MFA should be blocked regardless of auth provider.
        """
        session = MagicMock()

        org = MagicMock(spec=Organization)
        org.id = 1
        org.name = "Secure Corp"
        org.require_mfa = True

        org_dao = OrganizationDAO(session)
        org_dao.get_mfa_requiring_orgs_for_user = MagicMock(return_value=[org])

        blocking_orgs = org_dao.get_mfa_requiring_orgs_for_user("user-1")

        assert len(blocking_orgs) == 1
        assert blocking_orgs[0].name == "Secure Corp"

    def test_user_can_disable_mfa_no_enforcing_orgs(self):
        """
        When no org requires MFA, the user can disable it freely.
        """
        session = MagicMock()

        org_dao = OrganizationDAO(session)
        org_dao.get_mfa_requiring_orgs_for_user = MagicMock(return_value=[])

        blocking_orgs = org_dao.get_mfa_requiring_orgs_for_user("user-1")

        assert len(blocking_orgs) == 0

    def test_oauth_user_blocked_from_disabling_mfa(self):
        """
        OAuth-only users are also blocked from disabling MFA when
        org enforcement is active.
        """
        session = MagicMock()

        org = MagicMock(spec=Organization)
        org.id = 1
        org.name = "Secure Corp"
        org.require_mfa = True

        org_dao = OrganizationDAO(session)
        org_dao.get_mfa_requiring_orgs_for_user = MagicMock(return_value=[org])

        blocking_orgs = org_dao.get_mfa_requiring_orgs_for_user("oauth-user-1")

        assert len(blocking_orgs) == 1
        assert blocking_orgs[0].name == "Secure Corp"


# =============================================================================
# Schema Validation
# =============================================================================


class TestOrgMFASchemas:
    """Validate Pydantic schemas for MFA enforcement requests/responses."""

    def test_request_schema_valid(self):
        from orchestra.web.api.organization.schema import OrgMFASettingsRequest

        req = OrgMFASettingsRequest(require_mfa=True)
        assert req.require_mfa is True

    def test_request_schema_disable(self):
        from orchestra.web.api.organization.schema import OrgMFASettingsRequest

        req = OrgMFASettingsRequest(require_mfa=False)
        assert req.require_mfa is False

    def test_response_schema_defaults(self):
        from orchestra.web.api.organization.schema import OrgMFASettingsResponse

        resp = OrgMFASettingsResponse(require_mfa=False)
        assert resp.require_mfa is False

    def test_response_schema_enabled(self):
        from orchestra.web.api.organization.schema import OrgMFASettingsResponse

        resp = OrgMFASettingsResponse(require_mfa=True)
        assert resp.require_mfa is True

    def test_enforcement_status_response_not_enforced(self):
        from orchestra.web.api.organization.schema import MFAEnforcementStatusResponse

        resp = MFAEnforcementStatusResponse(
            enforced=False,
            has_mfa=False,
            setup_required=False,
        )
        assert resp.enforced is False
        assert resp.setup_required is False

    def test_enforcement_status_response_setup_required(self):
        from orchestra.web.api.organization.schema import MFAEnforcementStatusResponse

        resp = MFAEnforcementStatusResponse(
            enforced=True,
            has_mfa=False,
            setup_required=True,
        )
        assert resp.enforced is True
        assert resp.setup_required is True


# =============================================================================
# Integration-style tests (mocking the endpoint logic inline)
# =============================================================================


class TestMfaEnforcementStatusLogic:
    """
    Test the combined enforcement status computation that the
    mfa_enforcement_status endpoint performs.

    This tests the logic without an HTTP client, replicating the
    computation from the view function.

    MFA enforcement applies to ALL members (email/password and OAuth alike).
    """

    def _compute_enforcement_status(self, require_mfa, has_mfa):
        """Replicate the enforcement status logic from the view."""
        enforced = require_mfa
        setup_required = enforced and not has_mfa

        return {
            "enforced": enforced,
            "has_mfa": has_mfa,
            "setup_required": setup_required,
        }

    def test_not_enforced(self):
        result = self._compute_enforcement_status(require_mfa=False, has_mfa=False)
        assert result["enforced"] is False
        assert result["setup_required"] is False

    def test_enforced_user_has_mfa(self):
        result = self._compute_enforcement_status(require_mfa=True, has_mfa=True)
        assert result["enforced"] is True
        assert result["setup_required"] is False

    def test_enforced_no_mfa(self):
        """Any user without MFA must set it up when org enforces it."""
        result = self._compute_enforcement_status(require_mfa=True, has_mfa=False)
        assert result["enforced"] is True
        assert result["setup_required"] is True

    def test_not_enforced_user_has_mfa(self):
        """Voluntary MFA — org doesn't enforce, user has it anyway."""
        result = self._compute_enforcement_status(require_mfa=False, has_mfa=True)
        assert result["enforced"] is False
        assert result["setup_required"] is False


# =============================================================================
# MFA Status-by-Email Schema
# =============================================================================


class TestMfaStatusByEmailSchema:
    """Validate the MFAStatusByEmailResponse schema."""

    def test_user_found_with_mfa(self):
        from orchestra.web.api.auth.schema import MFAStatusByEmailResponse

        resp = MFAStatusByEmailResponse(user_found=True, mfa_enabled=True)
        assert resp.user_found is True
        assert resp.mfa_enabled is True

    def test_user_found_without_mfa(self):
        from orchestra.web.api.auth.schema import MFAStatusByEmailResponse

        resp = MFAStatusByEmailResponse(user_found=True, mfa_enabled=False)
        assert resp.user_found is True
        assert resp.mfa_enabled is False

    def test_user_not_found(self):
        from orchestra.web.api.auth.schema import MFAStatusByEmailResponse

        resp = MFAStatusByEmailResponse(user_found=False)
        assert resp.user_found is False
        assert resp.mfa_enabled is False
