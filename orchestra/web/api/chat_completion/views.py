from fastapi import APIRouter
from models.llm import CompletionsModel

from orchestra.web.api.chat_completion.schema import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)

router = APIRouter()


@router.post("/chat/completion", response_model=ChatCompletionResponse)
async def get_completions(request: ChatCompletionRequest) -> ChatCompletionResponse:
    """
    Get chat completions based on the request.

    :param request: ChatCompletionRequest object.

    :return: ChatCompletionResponse object.
    """
    language_model = CompletionsModel(
        provider=request.model.split("/")[0],
        model=request.model.split("/")[-1],
    )
    response = language_model.get_completion(
        messages=request.messages,
        temperature=request.temperature,
    )
    if not response:
        # TODO: Handle when response is None
        return ChatCompletionResponse(
            model=request.model,
            created=0,
            id="",
            choices=[],
            object="chat.completion",
            usage={},
        )
    if isinstance(response, ChatCompletionResponse):
        response.model = request.model
        return response
    usage = response["usage"].model_dump() if response["usage"] else None
    if response.get("choices", None):
        choices = []
        for choice in response.get("choices", None):
            choices.append(choice.model_dump())

    return ChatCompletionResponse(
        model=request.model,
        created=response.get("created", None),
        id=response.get("id", None),
        choices=choices,
        object=response.get("object", None),
        usage=usage,
    )
