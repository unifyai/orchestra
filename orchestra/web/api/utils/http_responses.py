import hashlib
from typing import List

from fastapi import HTTPException
from starlette import status

invalid_model_id = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=f"Invalid input. model-id doesn't match any entry in the model hub.",
)

invalid_model_str = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=(
        "Invalid model. The expected format is <model-id>@<provider>. "
        "See https://unify.ai/docs/hub/concepts/models.html "
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


invalid_api_key = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid API key. You can generate one at https://console.unify.ai/login",
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
