"""Prometheus counters for the billing pipeline."""
from prometheus_client import Counter

invoice_created_total = Counter(
    "invoice_created_total",
    "Stripe invoices created by the monthly invoicer",
)

invoice_paid_total = Counter(
    "invoice_paid_total",
    "Invoices reported PAID by Stripe webhook",
)

invoice_failed_total = Counter(
    "invoice_failed_total",
    "Invoices reported FAILED / ACTION_REQUIRED by Stripe webhook",
)

billing_suspended_total = Counter(
    "billing_suspended_total",
    "User accounts suspended by the daily billing-guard",
)
