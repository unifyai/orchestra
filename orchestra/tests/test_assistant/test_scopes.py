"""Unit tests for the scope catalog (orchestra.web.api.assistant.scopes)."""

from __future__ import annotations

import pytest

from orchestra.web.api.assistant.scopes import (
    GOOGLE_BASE_SCOPES,
    GOOGLE_SCOPE_BUNDLES,
    MICROSOFT_BASE_SCOPES,
    MICROSOFT_SCOPE_BUNDLES,
    available_features,
    build_scope_string,
    map_scopes_to_features,
)


class TestAvailableFeatures:

    def test_google_features(self):
        feats = available_features("google")
        assert set(feats) == set(GOOGLE_SCOPE_BUNDLES)

    def test_microsoft_features(self):
        feats = available_features("microsoft")
        assert set(feats) == set(MICROSOFT_SCOPE_BUNDLES)

    def test_microsoft_has_teams_and_sharepoint(self):
        feats = available_features("microsoft")
        assert "teams" in feats
        assert "sharepoint" in feats

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            available_features("yahoo")


class TestBuildScopeString:

    def test_google_email_only(self):
        result = build_scope_string("google", ["email"])
        parts = result.split()
        for base in GOOGLE_BASE_SCOPES:
            assert base in parts
        for scope in GOOGLE_SCOPE_BUNDLES["email"]:
            assert scope in parts

    def test_google_multi_feature(self):
        result = build_scope_string("google", ["email", "calendar"])
        parts = result.split()
        for scope in GOOGLE_SCOPE_BUNDLES["email"]:
            assert scope in parts
        for scope in GOOGLE_SCOPE_BUNDLES["calendar"]:
            assert scope in parts
        for base in GOOGLE_BASE_SCOPES:
            assert base in parts

    def test_google_no_duplicates(self):
        result = build_scope_string("google", ["email", "calendar"])
        parts = result.split()
        assert len(parts) == len(set(parts))

    def test_google_empty_features(self):
        result = build_scope_string("google", [])
        parts = result.split()
        assert parts == GOOGLE_BASE_SCOPES

    def test_microsoft_email_only(self):
        result = build_scope_string("microsoft", ["email"])
        parts = result.split()
        for base in MICROSOFT_BASE_SCOPES:
            if base == "offline_access":
                assert "offline_access" in parts
            else:
                assert f"https://graph.microsoft.com/{base}" in parts
        for scope in MICROSOFT_SCOPE_BUNDLES["email"]:
            assert f"https://graph.microsoft.com/{scope}" in parts

    def test_microsoft_offline_access_not_prefixed(self):
        result = build_scope_string("microsoft", ["email"])
        assert "https://graph.microsoft.com/offline_access" not in result
        assert "offline_access" in result

    def test_microsoft_multi_feature(self):
        result = build_scope_string("microsoft", ["email", "teams"])
        for scope in MICROSOFT_SCOPE_BUNDLES["teams"]:
            assert f"https://graph.microsoft.com/{scope}" in result

    def test_microsoft_empty_features(self):
        result = build_scope_string("microsoft", [])
        parts = result.split()
        assert "offline_access" in parts
        assert "https://graph.microsoft.com/User.Read" in parts
        assert len(parts) == 2

    def test_unknown_feature_raises(self):
        with pytest.raises(KeyError):
            build_scope_string("google", ["nonexistent"])


class TestMapScopesToFeatures:

    def test_google_email_fully_granted(self):
        scopes = " ".join(GOOGLE_BASE_SCOPES + GOOGLE_SCOPE_BUNDLES["email"])
        feats = map_scopes_to_features("google", scopes)
        assert "email" in feats

    def test_google_partial_bundle_not_listed(self):
        scopes = " ".join(
            GOOGLE_BASE_SCOPES + GOOGLE_SCOPE_BUNDLES["email"][:1],
        )
        feats = map_scopes_to_features("google", scopes)
        assert "email" not in feats

    def test_google_multiple_bundles(self):
        scopes = " ".join(
            GOOGLE_BASE_SCOPES
            + GOOGLE_SCOPE_BUNDLES["email"]
            + GOOGLE_SCOPE_BUNDLES["calendar"],
        )
        feats = map_scopes_to_features("google", scopes)
        assert sorted(feats) == ["calendar", "email"]

    def test_google_empty_string(self):
        assert map_scopes_to_features("google", "") == []

    def test_microsoft_email_fully_granted(self):
        prefixed = [
            f"https://graph.microsoft.com/{s}" for s in MICROSOFT_SCOPE_BUNDLES["email"]
        ]
        scopes = " ".join(
            ["https://graph.microsoft.com/User.Read", "offline_access"] + prefixed,
        )
        feats = map_scopes_to_features("microsoft", scopes)
        assert "email" in feats

    def test_microsoft_partial_bundle_not_listed(self):
        scopes = (
            "https://graph.microsoft.com/User.Read "
            "offline_access "
            "https://graph.microsoft.com/Mail.Read"
        )
        feats = map_scopes_to_features("microsoft", scopes)
        assert "email" not in feats

    def test_microsoft_teams(self):
        prefixed = [
            f"https://graph.microsoft.com/{s}" for s in MICROSOFT_SCOPE_BUNDLES["teams"]
        ]
        scopes = " ".join(
            ["https://graph.microsoft.com/User.Read", "offline_access"] + prefixed,
        )
        feats = map_scopes_to_features("microsoft", scopes)
        assert "teams" in feats

    def test_roundtrip_google(self):
        """build_scope_string → map_scopes_to_features recovers the features."""
        features = ["email", "drive", "tasks"]
        scope_str = build_scope_string("google", features)
        recovered = map_scopes_to_features("google", scope_str)
        assert sorted(recovered) == sorted(features)

    def test_roundtrip_microsoft(self):
        features = ["email", "calendar", "contacts"]
        scope_str = build_scope_string("microsoft", features)
        recovered = map_scopes_to_features("microsoft", scope_str)
        assert sorted(recovered) == sorted(features)
