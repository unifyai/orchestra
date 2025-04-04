import inspect
import logging
import os
from datetime import datetime, timezone

import litellm
import stripe
from anthropic import Anthropic
from openai import OpenAI

from orchestra.db.models.orchestra_models import Recharge

oai_func = OpenAI(api_key="").chat.completions.create
OPENAI_ALLOWED_ARGS = set(inspect.signature(oai_func).parameters.keys())

anth_func = Anthropic(api_key="").messages.create
ANTHROPIC_ALLOWED_ARGS = set(inspect.signature(anth_func).parameters.keys())

ADDITIONAL_ALLOWED_ARGS = {"aws_region_name", "vertex_location", "base_model"}


def filter_kwargs_for_openai_client(kwargs: dict) -> tuple[dict, dict]:
    extra_body = kwargs.get("extra_body", {})
    new_kwargs = {}
    allowed_args = OPENAI_ALLOWED_ARGS.union(ADDITIONAL_ALLOWED_ARGS)

    for k, v in kwargs.items():
        if k not in allowed_args:
            extra_body[k] = v
        else:
            new_kwargs[k] = v

    return new_kwargs, extra_body


def filter_kwargs_for_anthropic_client(kwargs: dict) -> tuple[dict, dict]:
    extra_body = kwargs.get("extra_body", {})
    new_kwargs = {}

    for k, v in kwargs.items():
        if k not in ANTHROPIC_ALLOWED_ARGS:
            extra_body[k] = v
        else:
            new_kwargs[k] = v

    return new_kwargs, extra_body


def filter_orchestra_only_args(arguments):
    return {
        k: v
        for k, v in arguments.items()
        if v is not None
        and k
        not in [
            "model",
            "messages",
            "signature",
            "use_custom_keys",
            "tags",
            "log_query_body",
            "log_response_body",
        ]
    }


def filter_request_params(arguments):
    """
    Filter argument parameters.

    :param arguments: arguments object.

    :return: dictionary of filtered parameters.
    """
    openai_params = [
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "max_tokens",
        "n",
        "presence_penalty",
        "response_format",
        "seed",
        "stop",
        "stream",
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "user",
        "stream",
    ]
    return {
        param: arguments.get(param)
        for param in openai_params
        if arguments.get(param) is not None
    }


def check_litellm_supported_args(kwargs, provider_endpoint):
    supported_params = litellm.get_supported_openai_params(provider_endpoint)
    if supported_params:
        supported_params = set(supported_params)
        for arg_name in kwargs:
            if arg_name not in supported_params.union(ADDITIONAL_ALLOWED_ARGS):
                logging.warning(
                    f"ArgumentWarning: {arg_name} not supported by {provider_endpoint}",
                )


def recharge_and_generate_invoice(user, users_dao):
    try:
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY_LIVE")
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
                f"Invoice {finalized_invoice.number} has been paid. Recording pending recharge.",
            )

            # Record the pending transaction in the Recharge table
            # Note: Credit will be added by the webhook handler when payment is confirmed
            recharge = Recharge(
                at=datetime.now(tz=timezone.utc),
                user_id=user.id,
                quantity=user.autorecharge_qty,
                type="invoice",
                transaction_id=finalized_invoice.id,
                status="pending",
            )
            users_dao.session.add(recharge)
            users_dao.session.commit()
        else:
            logging.warning(
                f"Invoice {finalized_invoice.number} did not pay as expected. Status: {pay_invoice.status}",
            )
            return

    except Exception as e:
        logging.error(f"An error occurred while generating the invoice: {str(e)}")
        return None
