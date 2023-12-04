from typing import List

from fastapi import APIRouter
from orchestra.web.api.schema import CompletionRequest, CompletionResponse
from models import Llama2Chat, Mistral

router = APIRouter()


def get_chat_model(request):
    if "llama" in request.model.split("/")[-1]:
        return Llama2Chat(provider=request.model.split("/")[0], model=request.model)
    elif "mistral" in request.model.split("/")[-1]:
        return Mistral(provider=request.model.split("/")[0], model=request.model)


@router.post("/chat/completion", response_model=CompletionResponse)
async def get_completions(request: CompletionRequest) -> CompletionResponse:
    language_model = get_chat_model(request)

    response = language_model.get_completion(
        messages=request.messages, temperature=request.temperature
    )

    returned_response = CompletionResponse(
        model=request.model,
        created=response["created"],
        choices=response["choices"],
        object=response["object"],
        usage=response["usage"],
    )

    completions = generate_completions(
        request.model, request.messages, request.temperature
    )

    return returned_response
