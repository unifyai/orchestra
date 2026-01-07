import hashlib
from typing import List

from fastapi import HTTPException
from starlette import status


class OutOfCreditError(RuntimeError):
    """Raised when a user runs out of credits."""


class AccountSuspendedError(RuntimeError):
    """Raised when a user's account is suspended due to billing issues."""


invalid_model_id = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="Invalid input. model-id doesn't match any entry in the model hub.",
)

invalid_model_str = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=(
        "Invalid model. The expected format is <model-id>@<provider>. "
        "See https://unify.ai/docs/hub/reference/endpoints.html#post-chat-completions "
        "for more information."
    ),
)

invalid_provider_str = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=(
        "Invalid provider. It must be a valid provider name or a valid metric "
        "configuration for dynamic routing."
        "See https://unify.ai/docs/hub/concepts/endpoints.html and "
        "https://unify.ai/docs/hub/concepts/runtime_routing.html "
        "for more information."
    ),
)

invalid_messages = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="Invalid input. Messages not in input.",
)

invalid_price_threshold = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=(
        "Invalid price threshold. Format needs to be config<[float][ic|oc]. "
        "See https://unify.ai/docs/hub/concepts/runtime_routing.html#thresholds for more details."
    ),
)


def invalid_optimisation_goal(performance_rules: List[str]):
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Invalid input. Provider has to be one of {performance_rules} when doing performance routing.",
    )


model_not_found = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=("Model not found"),
)

overspecified_model_provider = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=("You can only specify at most one of (model, provider)"),
)

invalid_api_key = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid API key. You can generate one at https://console.unify.ai/login",
)

account_frozen = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Your account has been suspended. Please reach out to hello@unify.ai if you have any questions.",
)

account_suspended = HTTPException(
    status_code=status.HTTP_402_PAYMENT_REQUIRED,
    detail=(
        "Your account has been suspended due to an unpaid invoice. "
        "Please update your payment method at https://console.unify.ai/ to resume service."
    ),
)

insufficient_credits_error = HTTPException(
    status_code=status.HTTP_402_PAYMENT_REQUIRED,
    detail=(
        "Whoops! It seems like this account doesn't have enough credits. "
        "To get a recharge, visit https://console.unify.ai/"
    ),
)

admin_not_authorized = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Admin access unauthorized, this incident will be reported.",
)


def not_found(item):
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{item} not found.",
    )


provider_not_found_under_conditions = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="No providers found within the specified thresholds.",
)


internal_endpoint_not_found = HTTPException(
    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    detail="Endpoint not found",
)


# TODO: Test this
def server_error_with_digest(text: str):
    digest = hashlib.shake_256(text.encode()).digest(4).hex()
    return (
        HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal Server Error. Digest: {digest}",
        ),
        digest,
    )
