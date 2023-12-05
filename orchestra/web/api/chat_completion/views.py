from fastapi import APIRouter
from models.llm import CompletionsModel

from orchestra.web.api.schema import CompletionRequest, CompletionResponse

router = APIRouter()


@router.post("/chat/completion", response_model=CompletionResponse)
async def get_completions(request: CompletionRequest) -> CompletionResponse:
    """
    Get chat completions based on the request.

    :param request: CompletionRequest object.

    :return: CompletionResponse object.
    """
    language_model = CompletionsModel(
        provider=request.model.split("/")[0],
        model=request.model,
    )

    response = language_model.get_completion(
        prompt=request.messages,
        temperature=request.temperature,
    )
    return CompletionResponse(
        model=request.model,
        created=response.get("created", None),
        choices=response.get("choices", None),
        object=response.get("object", None),
        usage=response.get("usage", None),
    )
