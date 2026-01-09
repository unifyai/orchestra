import json
import logging
import os
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


def recharge_and_generate_invoice(user, users_dao):
    try:
        # Configure Stripe API key
        stripe_key = os.environ.get("STRIPE_SECRET_KEY")
        if not stripe_key:
            logging.error("STRIPE_SECRET_KEY environment variable not set")
            return None

        stripe.api_key = stripe_key
        customer_id = user.stripe_customer_id
        customer = stripe.Customer.retrieve(customer_id)
        if not customer.invoice_settings.default_payment_method:
            logging.warning("Customer does not have a default payment method set.")
            return

        # Create an invoice with metadata at creation time
        invoice = stripe.Invoice.create(
            customer=customer_id,
            auto_advance=False,
            metadata={
                "user_id": user.id,
                "credits_purchased": user.autorecharge_qty,
            },
        )

        # Add an invoice item
        stripe.InvoiceItem.create(
            customer=customer_id,
            amount=int(user.autorecharge_qty * 100),  # stripe takes amount in cents
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
                    "user_id": user.id,
                    "credits_purchased": user.autorecharge_qty,
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
            # Since we paid immediately, mark it as PAID and add credits immediately
            recharge = Recharge(
                user_id=user.id,
                quantity=user.autorecharge_qty,
                amount_usd=Decimal(user.autorecharge_qty),  # 1 credit = $1
                invoice_group=month_end_utc(date.today()),
                type="invoice",
                transaction_id=finalized_invoice.id,
                status=RechargeStatus.PAID,  # Mark as PAID since we paid immediately
                stripe_invoice_id=finalized_invoice.id,
            )
            users_dao.session.add(recharge)

            # Add credits immediately since payment succeeded
            users_dao.recharge_credit(user.id, int(user.autorecharge_qty))
            users_dao.session.commit()
        else:
            logging.warning(
                f"Invoice {finalized_invoice.number} did not pay as expected. Status: {pay_invoice.status}",
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
