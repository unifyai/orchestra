"""Tests for phone number validation utility."""


from orchestra.web.api.utils.phone_number_validator import (
    PhoneNumberValidator,
    validate_phone_number,
)


class TestPhoneNumberValidator:
    """Tests for PhoneNumberValidator."""

    def test_valid_us_number_with_country_code(self):
        """Test valid US phone number with country code."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "+1-650-253-0000",
        )
        assert is_valid is True
        assert formatted == "+16502530000"
        assert error is None

    def test_valid_us_number_various_formats(self):
        """Test that various US number formats all normalize to E.164."""
        formats = [
            "+1 650 253 0000",
            "+1 (650) 253-0000",
            "+16502530000",
            "+1.650.253.0000",
        ]
        for phone in formats:
            is_valid, formatted, error = PhoneNumberValidator.validate_and_format(phone)
            assert is_valid is True, f"Failed for format: {phone}"
            assert formatted == "+16502530000", f"Wrong format for: {phone}"
            assert error is None

    def test_valid_us_number_with_default_region(self):
        """Test US number without country code using default region."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "650-253-0000",
            default_region="US",
        )
        assert is_valid is True
        assert formatted == "+16502530000"
        assert error is None

    def test_valid_uk_number(self):
        """Test valid UK phone number."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "+44 20 7946 0958",
        )
        assert is_valid is True
        assert formatted == "+442079460958"
        assert error is None

    def test_valid_german_number(self):
        """Test valid German phone number."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "+49 30 123456",
        )
        assert is_valid is True
        assert formatted == "+4930123456"
        assert error is None

    def test_valid_french_number(self):
        """Test valid French phone number."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "+33 1 23 45 67 89",
        )
        assert is_valid is True
        assert formatted == "+33123456789"
        assert error is None

    def test_valid_japanese_number(self):
        """Test valid Japanese phone number."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "+81 3 1234 5678",
        )
        assert is_valid is True
        assert formatted == "+81312345678"
        assert error is None

    def test_valid_australian_number(self):
        """Test valid Australian phone number."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "+61 2 1234 5678",
        )
        assert is_valid is True
        assert formatted == "+61212345678"
        assert error is None

    def test_invalid_number_too_short(self):
        """Test that too short numbers are rejected."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format("+1234")
        assert is_valid is False
        assert formatted is None
        assert error is not None

    def test_invalid_number_too_long(self):
        """Test that too long numbers are rejected."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "+1555123456789012345",
        )
        assert is_valid is False
        assert formatted is None
        assert error is not None

    def test_invalid_number_letters(self):
        """Test that pure letter strings are rejected."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "CALLMENOW",
        )
        assert is_valid is False
        assert formatted is None
        assert error is not None

    def test_invalid_number_nonsense(self):
        """Test that nonsense strings are rejected."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "not-a-phone",
        )
        assert is_valid is False
        assert formatted is None
        assert error is not None

    def test_invalid_country_code(self):
        """Test that invalid country codes are rejected."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "+999 123 456 7890",
        )
        assert is_valid is False
        assert formatted is None
        assert "country code" in error.lower() or "invalid" in error.lower()

    def test_empty_number(self):
        """Test that empty strings are rejected."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format("")
        assert is_valid is False
        assert formatted is None
        assert error == "Phone number is required"

    def test_whitespace_only(self):
        """Test that whitespace-only strings are rejected."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format("   ")
        assert is_valid is False
        assert formatted is None
        assert error is not None

    def test_strips_whitespace(self):
        """Test that leading/trailing whitespace is stripped."""
        is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
            "  +1 650 253 0000  ",
        )
        assert is_valid is True
        assert formatted == "+16502530000"
        assert error is None


class TestValidatePhoneNumberFunction:
    """Tests for the convenience function."""

    def test_valid_number_returns_dict(self):
        """Test that valid number returns proper dict structure."""
        result = validate_phone_number("+1-650-253-0000")
        assert result["is_valid"] is True
        assert result["formatted_phone_number"] == "+16502530000"
        assert result["error"] is None
        assert result["original_input"] == "+1-650-253-0000"

    def test_invalid_number_returns_dict(self):
        """Test that invalid number returns proper dict structure."""
        result = validate_phone_number("invalid")
        assert result["is_valid"] is False
        assert result["formatted_phone_number"] is None
        assert result["error"] is not None
        assert result["original_input"] == "invalid"

    def test_with_default_region(self):
        """Test convenience function with default region."""
        result = validate_phone_number("650-253-0000", default_region="US")
        assert result["is_valid"] is True
        assert result["formatted_phone_number"] == "+16502530000"

    def test_empty_returns_error(self):
        """Test that empty string returns error in dict."""
        result = validate_phone_number("")
        assert result["is_valid"] is False
        assert result["error"] == "Phone number is required"
