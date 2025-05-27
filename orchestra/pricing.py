"""Pricing utilities for credit conversion."""

from decimal import Decimal


def credits_to_usd(credits: int) -> Decimal:
    """Convert credits to USD amount.

    Args:
        credits: Number of credits to convert

    Returns:
        USD amount as Decimal (rate: $0.01 per credit)
    """
    return Decimal(credits) * Decimal("0.01")
