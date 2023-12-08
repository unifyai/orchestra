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
    # TODO: I think this should be the other way around (e.g. model/provider),
    # otherwise it can get confussing once we add the people that uploaded the model
    # imo (e.g. meta/llama-2-70b-chat/). Something like {uploaded_by}/{model}/{provider}
    # (we won't have uploaded_by and provider at the same time, but gives structure
    # I think) we could also have llama-2-70b-chat@replicate, which plays very nice
    # with the meaning of @ and removes confussion from the use of /
    language_model = CompletionsModel(
        provider=request.model.split("/")[0],
        model=request.model.split("/")[1],
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
