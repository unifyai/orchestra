from fastapi import APIRouter
from models.llm import CompletionsModel

from orchestra.web.api.chat_completion.schema import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)

router = APIRouter()


@router.post("/chat/completion", response_model=ChatCompletionRequest)
async def get_completions(request: ChatCompletionRequest) -> ChatCompletionResponse:
    """
    Get chat completions based on the request.

    :param request: ChatCompletionRequest object.

    :return: ChatCompletionResponse object.
    """
    language_model = CompletionsModel(
        provider=request.model.split("/")[0],
        model=request.model,
    )

    response = language_model.get_completion(
        messages=request.messages,
        temperature=request.temperature,
    )
    return ChatCompletionResponse(
        model=request.model,
        created=response.get("created", None),
        choices=response.get("choices", None),
        object=response.get("object", None),
        usage=response.get("usage", None),
    )
