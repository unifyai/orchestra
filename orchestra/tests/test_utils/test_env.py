"""
Tests for orchestra.env module - environment variable fallback handling.

Verifies that ORCHESTRA_* prefixed environment variables can fall back to
their standard (unprefixed) equivalents when the prefixed version is not set.
"""

import os
from unittest.mock import patch

import pytest

from orchestra.env import STANDARD_FALLBACKS, get_env, get_env_bool


class TestGetEnv:
    """Tests for get_env function."""

    def test_prefixed_takes_precedence(self):
        """ORCHESTRA_* prefixed var should take precedence over unprefixed."""
        with patch.dict(
            os.environ,
            {
                "ORCHESTRA_OPENAI_API_KEY": "prefixed-key",
                "OPENAI_API_KEY": "unprefixed-key",
            },
        ):
            result = get_env("ORCHESTRA_OPENAI_API_KEY")
            assert result == "prefixed-key"

    def test_fallback_to_unprefixed(self):
        """Should fall back to unprefixed var when prefixed is not set."""
        env = {"OPENAI_API_KEY": "unprefixed-key"}
        # Ensure ORCHESTRA_OPENAI_API_KEY is not set
        with patch.dict(os.environ, env, clear=True):
            result = get_env("ORCHESTRA_OPENAI_API_KEY")
            assert result == "unprefixed-key"

    def test_default_when_neither_set(self):
        """Should return default when neither prefixed nor unprefixed is set."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_env("ORCHESTRA_OPENAI_API_KEY", "default-value")
            assert result == "default-value"

    def test_none_when_neither_set_no_default(self):
        """Should return None when neither is set and no default provided."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_env("ORCHESTRA_OPENAI_API_KEY")
            assert result is None

    def test_no_fallback_for_internal_vars(self):
        """Internal Orchestra vars without fallbacks should not use fallback logic."""
        # ORCHESTRA_DB_USER is not in STANDARD_FALLBACKS, so no fallback should occur
        assert "ORCHESTRA_DB_USER" not in STANDARD_FALLBACKS

        with patch.dict(os.environ, {"DB_USER": "some-user"}, clear=True):
            # Even though DB_USER is set, it shouldn't be used as fallback
            result = get_env("ORCHESTRA_DB_USER")
            assert result is None

        with patch.dict(
            os.environ,
            {"ORCHESTRA_DB_USER": "orchestra-user"},
            clear=True,
        ):
            result = get_env("ORCHESTRA_DB_USER")
            assert result == "orchestra-user"


class TestGetEnvBool:
    """Tests for get_env_bool function."""

    def test_true_values(self):
        """Should recognize 'true' and '1' as True."""
        with patch.dict(os.environ, {"ORCHESTRA_OPENAI_API_KEY": "true"}):
            assert get_env_bool("ORCHESTRA_OPENAI_API_KEY") is True

        with patch.dict(os.environ, {"ORCHESTRA_OPENAI_API_KEY": "TRUE"}):
            assert get_env_bool("ORCHESTRA_OPENAI_API_KEY") is True

        with patch.dict(os.environ, {"ORCHESTRA_OPENAI_API_KEY": "1"}):
            assert get_env_bool("ORCHESTRA_OPENAI_API_KEY") is True

    def test_false_values(self):
        """Should return False for other values."""
        with patch.dict(os.environ, {"ORCHESTRA_OPENAI_API_KEY": "false"}):
            assert get_env_bool("ORCHESTRA_OPENAI_API_KEY") is False

        with patch.dict(os.environ, {"ORCHESTRA_OPENAI_API_KEY": "0"}):
            assert get_env_bool("ORCHESTRA_OPENAI_API_KEY") is False

        with patch.dict(os.environ, {"ORCHESTRA_OPENAI_API_KEY": "anything"}):
            assert get_env_bool("ORCHESTRA_OPENAI_API_KEY") is False

    def test_default_value(self):
        """Should return default when env var is not set."""
        with patch.dict(os.environ, {}, clear=True):
            assert get_env_bool("ORCHESTRA_OPENAI_API_KEY", default=True) is True
            assert get_env_bool("ORCHESTRA_OPENAI_API_KEY", default=False) is False

    def test_fallback_with_bool(self):
        """Should use fallback value for bool evaluation."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "true"}, clear=True):
            result = get_env_bool("ORCHESTRA_OPENAI_API_KEY")
            assert result is True


class TestStandardFallbacks:
    """Tests for the STANDARD_FALLBACKS mapping."""

    def test_api_key_fallbacks_exist(self):
        """OpenAI API key (used for embeddings) should have fallback defined."""
        expected_keys = [
            "ORCHESTRA_OPENAI_API_KEY",
        ]
        for key in expected_keys:
            assert key in STANDARD_FALLBACKS, f"Missing fallback for {key}"

    def test_fallback_values_are_standard_names(self):
        """Fallback values should be the standard (unprefixed) names."""
        assert STANDARD_FALLBACKS["ORCHESTRA_OPENAI_API_KEY"] == "OPENAI_API_KEY"


class TestAllProviderApiKeys:
    """Integration-style tests for API key fallbacks."""

    @pytest.mark.parametrize(
        "orchestra_key,standard_key",
        [
            ("ORCHESTRA_OPENAI_API_KEY", "OPENAI_API_KEY"),
        ],
    )
    def test_api_key_fallback(self, orchestra_key: str, standard_key: str):
        """Each API key should fall back to its standard name."""
        test_value = f"test-{standard_key}-value"

        # Test fallback
        with patch.dict(os.environ, {standard_key: test_value}, clear=True):
            result = get_env(orchestra_key)
            assert result == test_value, f"Fallback failed for {orchestra_key}"

        # Test precedence
        with patch.dict(
            os.environ,
            {orchestra_key: "prefixed", standard_key: "unprefixed"},
        ):
            result = get_env(orchestra_key)
            assert result == "prefixed", f"Precedence failed for {orchestra_key}"
