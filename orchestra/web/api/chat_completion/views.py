from fastapi import APIRouter
from models import Llama2Chat, Mistral

from orchestra.web.api.schema import CompletionRequest, CompletionResponse

router = APIRouter()


def get_chat_model(request):
    """
    Get the chat model based on the request.

    :param request: CompletionRequest object.
    :type request: CompletionRequest

    :return: Chat model instance (Llama2Chat or Mistral).
    :rtype: Union[Llama2Chat, Mistral]
    """
    if "llama" in request.model.split("/")[-1]:
        return Llama2Chat(provider=request.model.split("/")[0], model=request.model)
    elif "mistral" in request.model.split("/")[-1]:
        return Mistral(provider=request.model.split("/")[0], model=request.model)


@router.post("/chat/completion", response_model=CompletionResponse)
async def get_completions(request: CompletionRequest) -> CompletionResponse:
    """
    Get chat completions based on the request.

    :param request: CompletionRequest object.

    :return: CompletionResponse object.
    """
    language_model = get_chat_model(request)

    response = language_model.get_completion(
        messages=request.messages,
        temperature=request.temperature,
    )

    return CompletionResponse(
        model=request.model,
        created=response["created"],
        choices=response["choices"],
        object=response["object"],
        usage=response["usage"],
    )
