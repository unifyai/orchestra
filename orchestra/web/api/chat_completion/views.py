from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.web.api.schema import CompletionRequest, CompletionResponse

router = APIRouter()

@router.post("/chat/completion", response_model=CompletionResponse)
async def get_completions(
    request: CompletionRequest
) -> CompletionResponse:

    language_model = Llama2Chat(provider=request.model.split("/")[0], model=request.model)
    
    response = llama_model.get_completion(messages=request.messages, temperature=request.temperature)

    returned_response = CompletionResponse(model=request.model, created=response["created"], choices=response["choices"],
                        object=response["object"], usage=response["usage"])
    completions = generate_completions(request.model, request.messages, request.temperature)

    return returned_response