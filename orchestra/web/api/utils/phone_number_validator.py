"""
Phone number validation utilities using phonenumbers (Google's libphonenumber).

Validates phone numbers and normalizes them to E.164 format (+12345678901).
"""

from typing import Any, Dict, Optional, Tuple

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat


class PhoneNumberValidator:
    """Phone number validator using Google's libphonenumber."""

    @classmethod
    def validate_and_format(
        cls,
        phone_number: str,
        default_region: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Validate a phone number and format it to E.164.

        Args:
            phone_number: The phone number to validate (any format)
            default_region: Default region code (e.g., "US") if number lacks country code

        Returns:
            Tuple of (is_valid, formatted_number, error_message)
            - formatted_number is in E.164 format (+12345678901) if valid
        """
        if not phone_number:
            return False, None, "Phone number is required"

        phone_number = phone_number.strip()

        try:
            # Parse the phone number
            # If it starts with +, phonenumbers will detect the country
            # Otherwise, use default_region if provided
            parsed = phonenumbers.parse(phone_number, default_region)

            # Check if it's a valid number
            if not phonenumbers.is_valid_number(parsed):
                return False, None, "Invalid phone number"

            # Format to E.164 (+12345678901)
            formatted = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)

            return True, formatted, None

        except NumberParseException as e:
            error_messages = {
                NumberParseException.INVALID_COUNTRY_CODE: "Invalid country code",
                NumberParseException.NOT_A_NUMBER: "Not a valid phone number",
                NumberParseException.TOO_SHORT_AFTER_IDD: "Phone number too short",
                NumberParseException.TOO_SHORT_NSN: "Phone number too short",
                NumberParseException.TOO_LONG: "Phone number too long",
            }
            error_msg = error_messages.get(e.error_type, str(e))
            return False, None, error_msg


def validate_phone_number(
    phone_number: str,
    default_region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Convenience function to validate a phone number.

    Args:
        phone_number: The phone number to validate
        default_region: Default region code if number lacks country code

    Returns:
        Dict with validation results including E.164 formatted number
    """
    is_valid, formatted, error = PhoneNumberValidator.validate_and_format(
        phone_number,
        default_region,
    )

    return {
        "is_valid": is_valid,
        "formatted_phone_number": formatted,
        "error": error,
        "original_input": phone_number,
    }
