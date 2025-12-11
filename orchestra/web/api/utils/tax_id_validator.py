"""
Tax ID validation utilities using python-stdnum.

Provides comprehensive validation for various tax identification numbers
worldwide, including format validation and checksum verification where applicable.
"""

import importlib
import pkgutil
import re
from typing import Dict, List, Optional, Tuple

from stdnum.exceptions import InvalidChecksum, InvalidFormat, ValidationError

# Try to import stdnum for module discovery
try:
    import stdnum
except ImportError:
    stdnum = None


class TaxIDValidator:
    """Comprehensive tax ID validator using python-stdnum."""

    # Preferred module order for business tax IDs (first match wins)
    # VAT/GST are preferred for B2B billing
    PREFERRED_MODULES = [
        "vat",
        "gstin",
        "gst",
        "btw",
        "tva",
        "iva",
        "alv",
        "mva",
        "uid",
        "abn",
        "uen",
        "cnpj",
        "rfc",
        "ein",
        "tin",
        "nif",
        "cvr",
        "brn",
        "uscc",
        "rut",
        "inn",
        "trn",
    ]

    # Cache for discovered modules
    _COUNTRY_MODULES_CACHE: Optional[Dict[str, str]] = None
    _SUPPORTED_COUNTRIES_CACHE: Optional[Dict[str, str]] = None

    @classmethod
    def _discover_country_modules(cls) -> Dict[str, str]:
        """Auto-discover all available stdnum country modules."""
        if cls._COUNTRY_MODULES_CACHE is not None:
            return cls._COUNTRY_MODULES_CACHE

        modules = {}

        if stdnum is None:
            # Fallback if stdnum not available
            cls._COUNTRY_MODULES_CACHE = {}
            return {}

        # Iterate through all country packages in stdnum
        for importer, modname, ispkg in pkgutil.iter_modules(stdnum.__path__):
            # Handle 2-letter codes OR trailing underscore (e.g., 'in_' for India)
            # Python keyword countries like 'in' use 'in_' in stdnum
            if ispkg and (
                len(modname) == 2 or (len(modname) == 3 and modname.endswith("_"))
            ):
                # Convert module name to country code (e.g., 'in_' -> 'IN')
                country = modname.rstrip("_").upper()
                try:
                    country_pkg = importlib.import_module(f"stdnum.{modname}")
                    # Find all submodules for this country
                    available = []
                    for _, subname, _ in pkgutil.iter_modules(country_pkg.__path__):
                        available.append(subname)

                    # Pick the best module based on preference order
                    selected = None
                    for preferred in cls.PREFERRED_MODULES:
                        if preferred in available:
                            selected = preferred
                            break

                    # If no preferred module found, pick the first one
                    if selected is None and available:
                        selected = available[0]

                    if selected:
                        modules[country] = f"{modname}.{selected}"
                except Exception:
                    pass

        cls._COUNTRY_MODULES_CACHE = modules
        return modules

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
        tax_id = tax_id.strip()

        # Try EU VAT validation first for EU countries
        if cls._is_eu_country(country):
            is_valid, formatted_id, error = cls._validate_eu_vat(tax_id, country)
            if is_valid:
                return is_valid, formatted_id, error
            # If EU VAT fails, continue to try country-specific module

        # Discover available modules
        country_modules = cls._discover_country_modules()

        # Try country-specific module
        module_name = country_modules.get(country)
        if module_name:
            is_valid, formatted_id, error = cls._validate_with_module(
                tax_id, module_name
            )
            if is_valid:
                return is_valid, formatted_id, error
            # Return the error but continue to lenient fallback

        # Lenient fallback for unsupported countries or when strict validation fails
        return cls._lenient_validation(tax_id, country)

    @classmethod
    def validate_tax_id_strict(
        cls,
        tax_id: str,
        country: str,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Strictly validate a tax ID - no lenient fallback.

        Use this when you want to enforce proper tax ID formats.
        """
        if not tax_id or not country:
            return False, None, "Tax ID and country are required"

        country = country.upper()
        tax_id = tax_id.strip()

        # Try EU VAT validation first for EU countries
        if cls._is_eu_country(country):
            is_valid, formatted_id, error = cls._validate_eu_vat(tax_id, country)
            if is_valid:
                return is_valid, formatted_id, error

        # Discover available modules
        country_modules = cls._discover_country_modules()

        # Try country-specific module
        module_name = country_modules.get(country)
        if module_name:
            return cls._validate_with_module(tax_id, module_name)

        return False, None, f"No validation available for country: {country}"

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
        except Exception:
            return False, None, "EU VAT validation unavailable"

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
                validator = __import__(f"stdnum.{module_name}", fromlist=[""])

            # Clean the input (minimal cleaning - let stdnum handle format validation)
            clean_id = tax_id.strip()

            # Validate
            validated = validator.validate(clean_id)

            # Try to format if format function exists
            try:
                formatted = validator.format(validated)
            except (AttributeError, TypeError):
                formatted = validated

            return True, formatted, None

        except (ValidationError, InvalidFormat, InvalidChecksum) as e:
            return False, None, str(e)
        except Exception as e:
            return False, None, f"Validation error: {str(e)}"

    @classmethod
    def _lenient_validation(
        cls,
        tax_id: str,
        country: str,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Lenient validation for unsupported countries or fallback.

        Accepts alphanumeric tax IDs of reasonable length.
        This allows businesses from any country to register,
        with Stripe providing the final validation.
        """
        # Basic sanitization
        clean_id = re.sub(r"[\s\-\.]", "", tax_id).upper()

        # Must be alphanumeric
        if not re.match(r"^[A-Z0-9]+$", clean_id):
            return (
                False,
                None,
                "Tax ID must contain only letters, numbers, spaces, hyphens, or dots",
            )

        # Reasonable length (most tax IDs are 5-20 characters)
        if len(clean_id) < 5:
            return False, None, "Tax ID is too short (minimum 5 characters)"
        if len(clean_id) > 25:
            return False, None, "Tax ID is too long (maximum 25 characters)"

        # Return success with warning that it wasn't strictly validated
        return (
            True,
            clean_id,
            None,
        )

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
        if cls._SUPPORTED_COUNTRIES_CACHE is not None:
            return cls._SUPPORTED_COUNTRIES_CACHE

        supported = {}

        # Discover all country modules
        country_modules = cls._discover_country_modules()
        for country, module in country_modules.items():
            supported[country] = f"Full validation ({module})"

        # Add EU countries with VAT
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

        # Note: All other countries get lenient validation
        cls._SUPPORTED_COUNTRIES_CACHE = supported
        return supported

    @classmethod
    def get_validation_type(cls, country: str) -> str:
        """Get the type of validation available for a country."""
        country = country.upper()

        # Check discovered modules first (includes EU countries with specific modules)
        country_modules = cls._discover_country_modules()
        if country in country_modules:
            return "strict"

        # Then check EU VAT
        if cls._is_eu_country(country):
            return "eu_vat"

        return "lenient"

    @classmethod
    def clear_cache(cls):
        """Clear all caches (useful for testing)."""
        cls._COUNTRY_MODULES_CACHE = None
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
        "validation_type": TaxIDValidator.get_validation_type(country.upper()),
    }
