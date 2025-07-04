"""
Tax ID validation utilities using python-stdnum.

Provides comprehensive validation for various tax identification numbers
worldwide, including format validation and checksum verification where applicable.
"""

import re
from typing import Dict, Optional, Tuple

from stdnum.exceptions import InvalidChecksum, InvalidFormat, ValidationError


class TaxIDValidator:
    """Comprehensive tax ID validator using python-stdnum."""

    # Country code to stdnum module mapping for common tax ID types
    COUNTRY_MODULES = {
        "US": "us.ein",  # US Employer Identification Number
        "GB": "gb.vat",  # UK VAT number
        "AU": "au.abn",  # Australian Business Number
        "CA": "ca.gst_hst",  # Canadian GST/HST number
        "DE": "de.vat",  # German VAT (uses eu.vat)
        "FR": "fr.tva",  # French VAT (uses eu.vat)
        "IT": "it.iva",  # Italian VAT (uses eu.vat)
        "ES": "es.vat",  # Spanish VAT (uses eu.vat)
        "NL": "nl.btw",  # Dutch VAT (uses eu.vat)
        "BE": "be.vat",  # Belgian VAT (uses eu.vat)
        "AT": "at.uid",  # Austrian VAT (uses eu.vat)
        "SE": "se.vat",  # Swedish VAT (uses eu.vat)
        "DK": "dk.cvr",  # Danish VAT number
        "FI": "fi.alv",  # Finnish VAT (uses eu.vat)
        "IE": "ie.vat",  # Irish VAT (uses eu.vat)
        "PT": "pt.nif",  # Portuguese VAT (uses eu.vat)
        "NO": "no.mva",  # Norwegian VAT number
        "CH": "ch.vat",  # Swiss VAT number
        "JP": "jp.cn",  # Japanese Corporate Number
        "KR": "kr.brn",  # Korean Business Registration Number
        "IN": "in.gstin",  # Indian GST number
        "SG": "sg.uen",  # Singapore UEN
        "MY": "my.nric",  # Malaysian NRIC
        "TH": "th.moa",  # Thailand MOA number
        "BR": "br.cnpj",  # Brazilian CNPJ
        "MX": "mx.rfc",  # Mexican RFC number
        "RU": "ru.inn",  # Russian INN
        "CN": "cn.uscc",  # Chinese Unified Social Credit Code
    }

    # Cache for supported countries to avoid rebuilding on every call
    _SUPPORTED_COUNTRIES_CACHE = None

    @classmethod
    def validate_tax_id(
        cls,
        tax_id: str,
        country: str,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Validate a tax ID for a specific country.

        Args:
            tax_id: The tax ID number to validate
            country: Two-letter country code (ISO 3166-1 alpha-2)

        Returns:
            Tuple of (is_valid, formatted_tax_id, error_message)
        """
        if not tax_id or not country:
            return False, None, "Tax ID and country are required"

        country = country.upper()

        # Try EU VAT validation first for EU countries
        if cls._is_eu_country(country):
            is_valid, formatted_id, error = cls._validate_eu_vat(tax_id, country)
            if is_valid or error != "Module not found":
                return is_valid, formatted_id, error

        # Try country-specific module
        module_name = cls.COUNTRY_MODULES.get(country)
        if module_name:
            return cls._validate_with_module(tax_id, module_name)

        # Fallback to basic format validation
        return cls._basic_validation(tax_id, country)

    @classmethod
    def _validate_eu_vat(
        cls,
        tax_id: str,
        country: str,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Validate EU VAT numbers using the eu.vat module."""
        try:
            from stdnum.eu import vat

            # Clean the tax ID
            clean_id = tax_id.strip().replace(" ", "").replace("-", "").upper()

            # Add country prefix if not present
            if not clean_id.startswith(country):
                clean_id = country + clean_id

            # Validate
            validated = vat.validate(clean_id)
            formatted = vat.compact(validated)

            return True, formatted, None

        except (ValidationError, InvalidFormat, InvalidChecksum) as e:
            return False, None, str(e)
        except Exception as e:
            return False, None, "Module not found"

    @classmethod
    def _validate_with_module(
        cls,
        tax_id: str,
        module_name: str,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Validate using a specific stdnum module."""
        try:
            parts = module_name.split(".")
            if len(parts) == 2:
                country_mod = __import__(f"stdnum.{parts[0]}", fromlist=[parts[1]])
                validator = getattr(country_mod, parts[1])
            else:
                # Direct module import
                validator = __import__(f"stdnum.{module_name}", fromlist=[""])

            # Clean the input (minimal cleaning - let stdnum handle format validation)
            clean_id = tax_id.strip()

            # Validate
            validated = validator.validate(clean_id)

            # Try to format if format function exists
            try:
                formatted = validator.format(validated)
            except AttributeError:
                formatted = validated

            return True, formatted, None

        except (ValidationError, InvalidFormat, InvalidChecksum) as e:
            return False, None, str(e)
        except Exception as e:
            return False, None, f"Validation error: {str(e)}"

    @classmethod
    def _basic_validation(
        cls,
        tax_id: str,
        country: str,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Basic format validation using regex patterns."""
        patterns = {
            "US": r"^\d{2}-?\d{7}$",  # US EIN: 12-3456789
            "GB": r"^GB\d{9}$|^GB\d{12}$",  # UK VAT
            "AU": r"^\d{11}$",  # Australian ABN
            "CA": r"^\d{9}RT\d{4}$",  # Canadian GST/HST
            "CH": r"^CHE-?\d{3}\.?\d{3}\.?\d{3}$",  # Swiss
            "JP": r"^T?\d{13}$",  # Japanese
            "SG": r"^[0-9]{8}[A-Z]$|^[0-9]{9}[A-Z]$",  # Singapore UEN
        }

        pattern = patterns.get(country)
        if not pattern:
            return (
                False,
                None,
                f"No validation pattern available for country: {country}",
            )

        clean_id = tax_id.strip().replace(" ", "").replace("-", "").upper()

        if re.match(pattern, clean_id):
            return True, clean_id, None
        else:
            return False, None, f"Invalid format for {country} tax ID"

    @classmethod
    def _is_eu_country(cls, country: str) -> bool:
        """Check if a country is in the EU."""
        eu_countries = {
            "AT",
            "BE",
            "BG",
            "HR",
            "CY",
            "CZ",
            "DK",
            "EE",
            "FI",
            "FR",
            "DE",
            "GR",
            "HU",
            "IE",
            "IT",
            "LV",
            "LT",
            "LU",
            "MT",
            "NL",
            "PL",
            "PT",
            "RO",
            "SK",
            "SI",
            "ES",
            "SE",
        }
        return country.upper() in eu_countries

    @classmethod
    def get_supported_countries(cls) -> Dict[str, str]:
        """Get list of supported countries and their validation types."""
        # Use cached result if available
        if cls._SUPPORTED_COUNTRIES_CACHE is not None:
            return cls._SUPPORTED_COUNTRIES_CACHE

        supported = {}

        # Add countries with specific modules
        for country, module in cls.COUNTRY_MODULES.items():
            supported[country] = f"Full validation ({module})"

        # Add EU countries
        eu_countries = {
            "AT",
            "BE",
            "BG",
            "HR",
            "CY",
            "CZ",
            "DK",
            "EE",
            "FI",
            "FR",
            "DE",
            "GR",
            "HU",
            "IE",
            "IT",
            "LV",
            "LT",
            "LU",
            "MT",
            "NL",
            "PL",
            "PT",
            "RO",
            "SK",
            "SI",
            "ES",
            "SE",
        }

        for country in eu_countries:
            if country not in supported:
                supported[country] = "EU VAT validation"

        # Cache the result
        cls._SUPPORTED_COUNTRIES_CACHE = supported
        return supported

    @classmethod
    def clear_cache(cls):
        """Clear the supported countries cache (useful for testing)."""
        cls._SUPPORTED_COUNTRIES_CACHE = None


def validate_tax_id_for_country(tax_id: str, country: str) -> Dict[str, any]:
    """
    Convenience function to validate a tax ID.

    Args:
        tax_id: The tax ID to validate
        country: Two-letter country code

    Returns:
        Dict with validation results
    """
    is_valid, formatted_id, error = TaxIDValidator.validate_tax_id(tax_id, country)

    return {
        "is_valid": is_valid,
        "formatted_tax_id": formatted_id,
        "error": error,
        "country": country.upper(),
        "original_input": tax_id,
    }
