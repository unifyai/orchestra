"""
Business validation utilities for address and business information.

Provides standardized validation logic for business addresses and related data.
"""

from typing import Any, Dict, Optional

from orchestra.db.models.orchestra_models import AuthUser


def has_complete_business_address(user: AuthUser) -> bool:
    """
    Check if a user has a complete business address.

    A complete business address requires:
    - address_line1 (required)
    - city (required)
    - country (required)
    - state (optional but recommended)
    - postal_code (optional but recommended)

    Args:
        user: AuthUser instance

    Returns:
        bool: True if user has a complete business address
    """
    return all(
        [
            user.business_address_line1,
            user.business_city,
            user.business_country,
        ],
    )


def format_business_address(user: AuthUser) -> Optional[Dict[str, Any]]:
    """
    Format a business address for API responses.

    Args:
        user: AuthUser instance

    Returns:
        Dict containing formatted address or None if incomplete
    """
    if not has_complete_business_address(user):
        return None

    return {
        "address_line1": user.business_address_line1,
        "address_line2": user.business_address_line2,
        "city": user.business_city,
        "state": user.business_state,
        "country": user.business_country,
        "postal_code": user.business_postal_code,
    }


def validate_business_address_data(
    address_line1: Optional[str],
    city: Optional[str],
    country: Optional[str],
    state: Optional[str] = None,
    postal_code: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """
    Validate business address data.

    Args:
        address_line1: Street address line 1
        city: City name
        country: Country code or name
        state: State/province (optional)
        postal_code: Postal/ZIP code (optional)

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not address_line1:
        return False, "Address line 1 is required"

    if not city:
        return False, "City is required"

    if not country:
        return False, "Country is required"

    # Validate field lengths
    if len(address_line1) > 255:
        return False, "Address line 1 must be 255 characters or less"

    if len(city) > 100:
        return False, "City must be 100 characters or less"

    if len(country) > 100:
        return False, "Country must be 100 characters or less"

    if state and len(state) > 100:
        return False, "State must be 100 characters or less"

    if postal_code and len(postal_code) > 20:
        return False, "Postal code must be 20 characters or less"

    return True, None


def format_business_classification(user: AuthUser) -> Dict[str, Any]:
    """
    Format complete business classification data for API responses.

    Args:
        user: AuthUser instance

    Returns:
        Dict containing formatted business classification data
    """
    return {
        "account_type": user.account_type,
        "business_name": user.business_name,
        "tax_id": user.tax_id,
        "business_type": user.business_type,
        "business_verified": user.business_verified,
        "tax_exempt": user.tax_exempt,
        "tax_jurisdiction": user.tax_jurisdiction,
        "business_address": format_business_address(user),
    }
