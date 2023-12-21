from fastapi import APIRouter
from models.llm import CompletionsModel

from orchestra.web.api.predict.schema import PredictRequest, PredictResponse

router = APIRouter()


@router.post("/predict", response_model=PredictResponse)
async def get_prediction(request: PredictRequest) -> PredictResponse:
    """
    Get prediction result based on the request.

    :param request: PredictRequest object.

    :return: Model-specific PredictResponse object.
    """
    # TODO: Abstract this so that the modality and the task is
    # used to get the class (in this case, CompletionsModel)

    # TODO: Specify the arguments in the docs, discuss how are we going to define it
    # TODO: Add error 422 for incorrect arguments, model, or provider
    # TODO: Add error 500

    language_model = CompletionsModel(
        provider=request.provider,
        model=request.model,
    )
    response = language_model.get_completion(
        messages=request.arguments["messages"],
        temperature=request.arguments["temperature"],
    )
    if not response:
        # TODO: Handle when response is None
        return PredictResponse(
            response={
                "choices": [],
                "usage": {},
            },
        )

    usage = response["usage"].model_dump() if response["usage"] else None
    if response.get("choices", None):
        choices = []
        for choice in response.get("choices", None):
            choices.append(choice.model_dump())

    return PredictResponse(
        response={
            "choices": choices,
            "usage": usage,
        },
    )
