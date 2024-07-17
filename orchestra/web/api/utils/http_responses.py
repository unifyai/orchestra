import hashlib
from typing import List

from fastapi import HTTPException
from starlette import status

router_already_deployed = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="This router is already deployed!",
)

router_is_not_deployed = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="This router is not deployed!",
)


def invalid_training_endpoints(endpoints: List[str]):
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Invalid input. Couldn't find endpoints {endpoints}.",
    )


router_training_already_exists = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=(
        "A router with this name has already been trained. Please, "
        "choose a different one."
    ),
)

router_training_does_not_exist = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail=(
        "This router training doesn't exist. "
        "Please, choose a different one or trigger the training first."
    ),
)

invalid_dataset_name = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="Invalid name for a dataset. Please, choose a different one.",
)

dataset_already_exists = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="A dataset with this name already exists. Please, choose a different one.",
)

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

user_id_not_found = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Specified user-id not found.",
)

dataset_does_not_exist = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="This dataset does not exist.",
)

evaluation_does_not_exist = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="This evaluation does not exist.",
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
