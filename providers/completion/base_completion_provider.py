import json
import logging
from typing import Any, Dict, List, Optional

import litellm
import openai

logger = logging.getLogger(__name__)


class BaseCompletionProvider:
    """Base class for completion providers."""

    # TODO: Make this a property and enforce definition with NotImplemented
    supported_models: Dict[str, Any] = {}

    def __init__(self) -> None:
        self.model: str = ""

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        litellm.api_key = api_key

    def get_cost_max(self, model: str) -> float:  # noqa: D102
        if model not in self.supported_models:
            raise ValueError("Model not supported")
        return (
            self.supported_models[model]["cost"]["completion"]
            / 1e6
            * self.supported_models[model]["context_window"]
        )
    
    def compute_cost(self, model:str, prompt_tokens: int, completion_tokens: int) -> float:
        return (self.supported_models[model]["cost"]["prompt"] * prompt_tokens
            + self.supported_models[model]["cost"]["completion"] * completion_tokens) / 1e6

    def complete(  # noqa: D102, WPS211, C901
        self,
        model: str,
        messages: List,  # type: ignore
        max_tokens: Optional[int] = 512,
        temperature: Optional[float] = 0.9,
        stream: Optional[bool] = False,
    ) -> Optional[Any]:
        if model not in self.supported_models:
            raise ValueError("Model not supported")

        if isinstance(self.supported_models, dict):
            provider_model_endpoint = self.supported_models[model]["endpoint"]
        else:
            provider_model_endpoint = model

        try:
            if stream:
                return self.async_generator_wrapper(
                    litellm.completion(
                        model=provider_model_endpoint,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        stream=True,
                    ),
                    model,
                )
            response = litellm.completion(
                model=provider_model_endpoint,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if not isinstance(response["usage"], dict) and response["usage"]:
                usage = response["usage"].model_dump()
            elif response["usage"]:
                usage = response["usage"]
            else:
                usage = None
            return response, self.compute_cost(model, usage["prompt_tokens"], usage["completion_tokens"])
        except openai.APIError as error:
            logger.error(f"Raised openai.APIError, Error: {error}")
        except openai.APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None

    async def update_credits(self, cost):
        print("total cost was ", cost)

    async def async_generator_wrapper(  # noqa: D102, WPS210, WPS231, WPS210
        self,
        response,
        model,
    ):
        total_cost = 0
        try:
            for part in response:
                if not isinstance(  # noqa: WPS337, E501
                    part.get("usage", None),
                    dict,
                ) and part.get(
                    "usage",
                    None,  # noqa: C812
                ):
                    usage = part["usage"].model_dump()
                elif part.get("usage", None):
                    usage = part["usage"]
                else:
                    usage = None
                print("whole part is  ", part)
                # total_cost += self.compute_cost(model, usage["prompt_tokens"], usage["completion_tokens"])
                choices = []
                if part.get("choices", None):
                    for choice in part.get("choices", None):
                        choices.append(choice.model_dump())
                part_dict = {
                    "model": model,
                    "created": part.get("created", None),
                    "id": part["id"],
                    "choices": choices,
                    "object": part.get("object", "chat.completion"),
                    "usage": usage,
                }
                part_json = json.dumps(part_dict)
                yield part_json
        finally:
            await self.update_credits(total_cost)

                
