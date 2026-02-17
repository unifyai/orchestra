"""
Business validation utilities for billing.

Provides:
- Address validation (``validate_billing_address_data``)
- Stripe tax ID type mapping (``get_stripe_tax_id_type``, ``get_stripe_tax_id_data``)
- Stripe tax-exempt status logic (``get_stripe_tax_exempt_status``)
- Stripe customer name field builder (``build_stripe_customer_name``)
- Stripe tax ID sync helper (``sync_tax_id_to_stripe``)
"""

from typing import Any, Dict, Optional, Tuple


def validate_billing_address_data(
    line1: Optional[str],
    city: Optional[str],
    country: Optional[str],
    line2: Optional[str] = None,
    state: Optional[str] = None,
    postal_code: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Validate billing address data.

    Args:
        line1: Street address line 1
        city: City name
        country: Two-letter ISO country code
        line2: Street address line 2 (optional)
        state: State/province (optional)
        postal_code: Postal/ZIP code (optional)

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not line1:
        return False, "Address line 1 is required"

    if not city:
        return False, "City is required"

    if not country:
        return False, "Country is required"

    # Validate country code format (ISO 3166-1 alpha-2)
    if len(country) != 2:
        return False, "Country must be a 2-letter ISO code (e.g., 'US', 'GB')"

    # Validate field lengths
    if len(line1) > 255:
        return False, "Address line 1 must be 255 characters or less"

    if line2 and len(line2) > 255:
        return False, "Address line 2 must be 255 characters or less"

    if len(city) > 100:
        return False, "City must be 100 characters or less"

    if state and len(state) > 100:
        return False, "State must be 100 characters or less"

    if postal_code and len(postal_code) > 20:
        return False, "Postal code must be 20 characters or less"

    return True, None


def get_stripe_tax_id_type(country_code: Optional[str]) -> str:
    """
    Determine the Stripe tax ID type based on country code.

    Stripe requires specific tax ID types per country. This function maps
    ISO 3166-1 alpha-2 country codes to the appropriate Stripe tax ID type.

    Args:
        country_code: ISO 3166-1 alpha-2 country code (e.g., "US", "GB", "DE")

    Returns:
        Stripe tax ID type string (e.g., "us_ein", "gb_vat", "eu_vat")
    """
    if not country_code:
        return "eu_vat"  # Default

    country_code = country_code.upper()

    # Country-specific tax ID types
    tax_type_map = {
        "GB": "gb_vat",
        "AU": "au_abn",
        "US": "us_ein",
        "CA": "ca_gst_hst",
        "IN": "in_gst",
        "NZ": "nz_gst",
        "SG": "sg_gst",
        "CH": "ch_vat",
        "NO": "no_vat",
        "JP": "jp_cn",
        "KR": "kr_brn",
        "MX": "mx_rfc",
        "BR": "br_cnpj",
        "ZA": "za_vat",
    }

    # EU countries use eu_vat
    eu_countries = {
        "AT",
        "BE",
        "BG",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }

    if country_code in tax_type_map:
        return tax_type_map[country_code]
    if country_code in eu_countries:
        return "eu_vat"

    # Default to eu_vat for unknown countries
    return "eu_vat"


def get_stripe_tax_id_data(
    tax_id: Optional[str],
    country_code: Optional[str],
) -> Optional[list]:
    """
    Build tax_id_data parameter for Stripe Customer creation.

    Args:
        tax_id: The tax ID value (e.g., VAT number)
        country_code: ISO 3166-1 alpha-2 country code

    Returns:
        List of tax ID data dicts for Stripe API, or None if no tax ID
    """
    if not tax_id:
        return None

    tax_id_type = get_stripe_tax_id_type(country_code)
    return [{"type": tax_id_type, "value": tax_id}]


def get_stripe_tax_exempt_status(
    tax_id: Optional[str],
    country_code: Optional[str],
) -> str:
    """
    Determine Stripe tax_exempt status based on tax ID and country.

    Per Stripe's tax system:
    - "none": Normal tax calculation (default for individuals)
    - "reverse": B2B reverse-charge (customer self-assesses tax, e.g. EU VAT
      intra-community supplies, or cross-border B2B in many jurisdictions)
    - "exempt": Fully exempt from tax

    We use "reverse" when a valid tax ID is provided (indicates B2B),
    and "none" otherwise. "exempt" is intentionally not auto-applied.

    Args:
        tax_id: The customer's tax ID value (e.g., VAT number)
        country_code: ISO 3166-1 alpha-2 country code

    Returns:
        Stripe tax_exempt string: "none" or "reverse"
    """
    if not tax_id:
        return "none"

    # Countries where a tax ID indicates B2B reverse-charge eligibility
    tax_id_type = get_stripe_tax_id_type(country_code)
    # VAT-type IDs trigger reverse charge; non-VAT IDs (e.g. US EIN) do not
    reverse_charge_types = {
        "eu_vat",
        "gb_vat",
        "ch_vat",
        "no_vat",
        "is_vat",
        "au_abn",
        "nz_gst",
        "sg_gst",
        "in_gst",
        "ca_gst_hst",
    }
    if tax_id_type in reverse_charge_types:
        return "reverse"

    return "none"


def build_stripe_customer_name(
    *,
    is_business: bool,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build Stripe name fields using the newer individual_name / business_name
    fields alongside the legacy ``name`` field.

    Stripe Customer now supports three name fields:
    - ``name``: Generic (max 256) — always populated for backward compat
    - ``individual_name``: Personal name (max 150)
    - ``business_name``: Company name (max 150)

    Args:
        is_business: True if this is a business/org account.
        name: The display name.

    Returns:
        Dict with appropriate Stripe name fields.
    """
    params: Dict[str, Any] = {}
    if not name:
        return params

    # Always set the generic name field for backward compat
    params["name"] = name[:256]

    if is_business:
        params["business_name"] = name[:150]
    else:
        params["individual_name"] = name[:150]

    return params


def sync_tax_id_to_stripe(
    stripe_customer_id: str,
    tax_id: Optional[str],
    country_code: Optional[str],
    *,
    logger: Any = None,
) -> None:
    """
    Sync a tax ID to a Stripe customer (delete old → create new).

    Also updates tax_exempt based on the new tax ID state.

    Args:
        stripe_customer_id: Stripe customer ID.
        tax_id: New tax ID value (None to clear).
        country_code: ISO country code for determining tax ID type.
        logger: Optional logger instance.
    """
    import stripe

    try:
        # 1. Delete existing tax IDs
        existing_tax_ids = stripe.Customer.list_tax_ids(
            stripe_customer_id,
            limit=10,
        )
        for existing in existing_tax_ids.data:
            try:
                stripe.Customer.delete_tax_id(stripe_customer_id, existing.id)
            except stripe.error.StripeError:
                pass  # Ignore deletion errors

        # 2. Create new tax ID if provided
        if tax_id:
            tax_id_type = get_stripe_tax_id_type(country_code)
            stripe.Customer.create_tax_id(
                stripe_customer_id,
                type=tax_id_type,
                value=tax_id,
            )

        # 3. Update tax_exempt status
        tax_exempt = get_stripe_tax_exempt_status(tax_id, country_code)
        stripe.Customer.modify(
            stripe_customer_id,
            tax_exempt=tax_exempt,
        )

    except stripe.error.InvalidRequestError as e:
        if logger:
            logger.warning(
                f"Could not sync tax ID to Stripe for {stripe_customer_id}: {e}",
            )
    except stripe.error.StripeError as e:
        if logger:
            logger.warning(
                f"Stripe error syncing tax ID for {stripe_customer_id}: {e}",
            )
