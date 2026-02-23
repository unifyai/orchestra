import json
import logging
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

import numpy as np
import stripe

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.lib.time import month_end_utc
from orchestra.settings import settings


def recharge_and_generate_invoice(billing_account, db_session):
    """
    Generate a Stripe invoice for auto-recharge and credit a BillingAccount.

    Works identically for user and organization billing accounts.

    Args:
        billing_account: The BillingAccount to recharge (must have stripe_customer_id).
        db_session: SQLAlchemy session for DB operations.
    """
    try:
        # Configure Stripe API key
        if not settings.stripe_secret_key:
            logging.error("stripe_secret_key not configured in settings")
            return None

        stripe.api_key = settings.stripe_secret_key

        if not billing_account or not billing_account.stripe_customer_id:
            logging.error(
                f"BillingAccount {getattr(billing_account, 'id', '?')} "
                f"has no stripe_customer_id",
            )
            return None

        customer_id = billing_account.stripe_customer_id
        autorecharge_qty = float(billing_account.autorecharge_qty)
        ba_id = billing_account.id

        customer = stripe.Customer.retrieve(customer_id)
        if not customer.invoice_settings.default_payment_method:
            logging.warning("Customer does not have a default payment method set.")
            return

        # Deterministic idempotency key to prevent duplicate invoices on retries
        invoice_group = month_end_utc(date.today())
        idem_key = f"autorecharge-ba{ba_id}-{invoice_group}-{int(autorecharge_qty)}"

        # Build invoice params with tax support
        invoice_params = {
            "customer": customer_id,
            "auto_advance": False,
            "automatic_tax": {"enabled": True},
            "description": (f"Auto-recharge: {int(autorecharge_qty)} credits"),
            "payment_settings": {
                "payment_method_options": {
                    "card": {"request_three_d_secure": "any"},
                },
            },
            "metadata": {
                "billing_account_id": str(ba_id),
                "credits_purchased": autorecharge_qty,
                "type": "auto_recharge",
            },
        }

        # Include customer tax IDs if available on the billing account
        if billing_account.tax_id:
            from orchestra.web.api.utils.business_validation import (
                get_stripe_tax_id_type,
            )

            tax_id_type = billing_account.tax_id_type
            if not tax_id_type:
                country = None
                if billing_account.billing_address and isinstance(
                    billing_account.billing_address,
                    dict,
                ):
                    country = billing_account.billing_address.get("country")
                tax_id_type = get_stripe_tax_id_type(country)

            invoice_params["customer_tax_ids"] = [
                {"type": tax_id_type, "value": billing_account.tax_id},
            ]

        invoice = stripe.Invoice.create(
            **invoice_params,
            idempotency_key=idem_key,
        )

        # Add an invoice item
        stripe.InvoiceItem.create(
            customer=customer_id,
            amount=int(autorecharge_qty * 100),  # stripe takes amount in cents
            currency="usd",
            description="Unify Credits",
            invoice=invoice.id,
        )

        # Finalize the invoice, which will automatically create a PaymentIntent if needed
        finalized_invoice = stripe.Invoice.finalize_invoice(invoice.id)
        payment_intent_id = finalized_invoice.payment_intent

        if payment_intent_id:
            stripe.PaymentIntent.modify(
                payment_intent_id,
                metadata={
                    "billing_account_id": str(ba_id),
                    "credits_purchased": autorecharge_qty,
                },
            )
        logging.info(f"Finalized invoice: {finalized_invoice}")

        # Pay the invoice
        pay_invoice = stripe.Invoice.pay(invoice.id)

        if pay_invoice.status == "paid":
            logging.info(
                f"Invoice {finalized_invoice.number} has been paid. Recording paid recharge.",
            )

            # Record the paid transaction in the Recharge table
            recharge = Recharge(
                billing_account_id=ba_id,
                quantity=autorecharge_qty,
                amount_usd=Decimal(str(autorecharge_qty)),  # 1 credit = $1
                invoice_group=invoice_group,
                type="invoice",
                transaction_id=finalized_invoice.id,
                status=RechargeStatus.PAID,
                stripe_invoice_id=finalized_invoice.id,
            )
            db_session.add(recharge)

            # Add credits directly to the billing account
            billing_account.credits += Decimal(str(int(autorecharge_qty)))
            db_session.commit()
        else:
            logging.warning(
                f"Invoice {finalized_invoice.number} did not pay as expected. "
                f"Status: {pay_invoice.status}",
            )
            return

    except Exception as e:
        logging.error(f"An error occurred while generating the invoice: {str(e)}")
        return None


class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, np.generic):
            return obj.item()
        elif isinstance(obj, time):
            return obj.strftime("%H:%M:%S.%f")
        elif isinstance(obj, date):
            # Handle both datetime and date objects
            if isinstance(obj, datetime):
                # Return ISO format with timezone info if available
                if obj.tzinfo is not None:
                    return obj.isoformat()
                return obj.replace(tzinfo=timezone.utc).isoformat()
            # For plain date objects
            return obj.isoformat()
        elif isinstance(obj, timedelta):
            # Convert to ISO 8601 duration format
            # Format: P[n]Y[n]M[n]DT[n]H[n]M[n]S
            total_seconds = obj.total_seconds()
            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            seconds = total_seconds % 60

            # Build duration string
            duration = "P"
            if obj.days:
                duration += f"{obj.days}D"

            # Add time part if there are hours, minutes, or seconds
            if hours or minutes or seconds:
                duration += "T"
                if hours:
                    duration += f"{hours}H"
                if minutes:
                    duration += f"{minutes}M"
                if seconds:
                    # Handle fractional seconds
                    if seconds == int(seconds):
                        duration += f"{int(seconds)}S"
                    else:
                        duration += f"{seconds:g}S"  # :g removes trailing zeros

            # Handle zero duration edge case
            if duration == "P":
                duration = "PT0S"

            return duration
        elif isinstance(obj, uuid.UUID):
            return str(obj)
        elif isinstance(obj, Path):
            return str(obj)
        elif isinstance(obj, (set, frozenset)):
            return list(obj)
        elif isinstance(obj, (bytes, bytearray)):
            return obj.decode("utf-8", errors="replace")
        elif isinstance(obj, Enum):
            return obj.value
        elif is_dataclass(obj):
            return asdict(obj)

        return super().default(obj)
