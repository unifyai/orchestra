import logging
import uuid
from typing import Any, Dict, List, Optional

import litellm
import openai
import tiktoken
from litellm.utils import ModelResponse, Usage

logger = logging.getLogger(__name__)

# Pricing info of providers with pay-per-token model is
# standardized to per million tokens.
PRICING_PER_TOKENS = 1000000


class BaseCompletionProvider:
    """Base class for completion providers."""

    # TODO: Make this a property and enforce definition with NotImplemented
    supported_models: Dict[str, Any] = {}

    def __init__(self) -> None:
        self.model: str = ""

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        litellm.api_key = api_key

    def get_cost_max(self, model_name: str) -> float:  # noqa: D102
        if model_name not in self.supported_models:
            raise ValueError("Model not supported")
        return (
            self.supported_models[model_name]["cost"]["completion"]
            * self.supported_models[model_name]["context_window"]
            / PRICING_PER_TOKENS
        )

    def compute_cost(
        self,
        model_name: str,
        prompts: List[str],
        response: ModelResponse,
    ) -> float:
        """
        Compute the cost of a completion.

        :param model_name: The model to use for completion.
        :param prompts: List of the prompt texts.
        :param response: Model response from LiteLLM completion.

        :return: The cost of the completion.
        """
        cost_data = self.supported_models[model_name]["cost"]  # type: ignore
        prompt_cost = 0
        if cost_data.get("hardware"):
            cost_data = self.supported_models[model_name]["cost"]  # type: ignore
            return (
                self.hardware_pricing_per_sec[cost_data["hardware"]]  # type: ignore
                * response._response_ms
                / 1000
            )
        if cost_data.get("online"):
            prompt_cost += cost_data["online"]["charge_per_1000_requests"] / 1000
        prompt_cost += (
            response.usage["prompt_tokens"] * cost_data["prompt"] / PRICING_PER_TOKENS
        )
        completion_cost = (
            response.usage["completion_tokens"]
            * cost_data["completion"]
            / PRICING_PER_TOKENS
        )
        return prompt_cost + completion_cost

    def compute_cost_streaming(  # noqa: WPS210
        self,
        model: str,
        completions: str,
        messages: List[Dict],
    ) -> float:
        """
        Compute the cost of a completion when streaming.

        :param model: The model to use for completion.
        :type model: str
        :param completions: The completed text.
        :type completions: str
        :param messages: List of input prompts.
        :type messages: List[Dict]

        :return: The cost of the completion.
        """
        try:
            total_prompt = ""
            for item in messages:  # noqa: WPS519
                total_prompt += item["content"]
            encoding = tiktoken.get_encoding("cl100k_base")
            tokens = encoding.encode(total_prompt)
            prompt_tokens = len(tokens)

            tokens = encoding.encode(completions)
            completion_tokens = len(tokens)
            response = ModelResponse(usage=Usage(prompt_tokens, completion_tokens))

            return self.compute_cost(
                model,
                [item["content"] for item in messages],  # noqa: WPS441
                response,
            )
        except Exception:
            return 0

    def complete(  # noqa: D102, WPS211, C901, WPS231
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
                return (
                    AsyncGeneratorWrapper(
                        litellm.completion(
                            model=provider_model_endpoint,
                            messages=messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            stream=True,
                        ),
                        model,
                        messages,
                        compute_cost_streaming=self.compute_cost_streaming,
                    ),
                    None,
                )
            response = litellm.completion(
                model=provider_model_endpoint,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            return response, self.compute_cost(
                model,
                [item["content"] for item in messages],
                response,
            )
        except openai.APIError as error:
            logger.error(f"Raised openai.APIError, Error: {error}")
        except openai.APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None


class AsyncGeneratorWrapper:  # noqa: D101
    def __init__(  # noqa: WPS211
        self,
        response,
        model,
        messages,
        compute_cost_streaming,
        compute_cost=None,  # noqa: WPS211, E501
    ):
        self._response = response
        self._model = model
        self._messages = messages
        self._compute_cost_streaming = compute_cost_streaming
        self._compute_cost = compute_cost
        self.total_cost = None

    async def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = ""
        usage = {}
        try:  # noqa: WPS501
            for part in self._response:
                usage = part.get("usage", {})

                choices = [
                    getattr(choice, "model_dump", lambda: None)()
                    for choice in part.get("choices", [])
                ]

                part_dict = {
                    "model": self._model,
                    "created": part.get("created", None),
                    "id": part.get(
                        "id",
                        f"chatcmpl-{str(uuid.uuid4())}",  # noqa: WPS237, E501
                    ),
                    "choices": choices,
                    "object": part.get("object", "chat.completion.chunk"),
                    "usage": usage.model_dump() if isinstance(usage, Usage) else usage,
                }
                part_text = choices[0]["delta"]["content"]
                whole += part_text if part_text else ""
                yield part_dict
        finally:
            if isinstance(usage, Usage):
                self.total_cost = self._compute_cost(
                    self._model,
                    [item["content"] for item in self._messages],
                    ModelResponse(usage=usage),
                )
            else:
                self.total_cost = self._compute_cost_streaming(
                    self._model,
                    whole,
                    self._messages,
                )
