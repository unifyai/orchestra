from fastapi import APIRouter
from models.llm import CompletionsModel

from orchestra.web.api.query.schema import QueryRequest, QueryResponse

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
async def get_query(request: QueryRequest) -> QueryResponse:
    """
    Get query result based on the request.

    :param request: QueryRequest object.

    :return: Model-specific QueryResponse object.
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
        return QueryResponse(
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

    return QueryResponse(
        response={
            "choices": choices,
            "usage": usage,
        },
    )
