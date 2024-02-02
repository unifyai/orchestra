import datetime
import logging
from typing import List, Optional

import openai
from litellm.utils import ModelResponse, Usage
from octoai.chat import ChatCompletion
from octoai.client import Client
from providers.completion.base_completion_provider import (
    AsyncGeneratorWrapper,
    BaseCompletionProvider,
)

from orchestra.web.api.chat_completion.schema import ChatCompletionResponse

logger = logging.getLogger(__name__)


class OctoAI(BaseCompletionProvider):
    """
    A completion provider that uses the OctoAI service.

    Supported models: https://docs.octoai.cloud/docs/text-generation
    Pricing: https://docs.octoai.cloud/docs/pricing (below are per million tokens)
    """

    supported_models = {
        "llama-2-70b-chat": {
            "endpoint": "llama-2-70b-chat-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.6, "completion": 1.9},
        },
        "llama-2-70b-chat-int4": {
            "endpoint": "llama-2-70b-chat-int4",
            "context_window": 4096,
            "cost": {"prompt": 0.6, "completion": 1.2},
        },
        "llama-2-13b-chat": {
            "endpoint": "llama-2-13b-chat-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.5},
        },
        "codellama-34b-instruct": {
            "endpoint": "codellama-34b-instruct-fp16",
            "context_window": 16384,
            "cost": {"prompt": 0.5, "completion": 1.15},
        },
        "codellama-34b-instruct-int4": {
            "endpoint": "codellama-34b-instruct-int4",
            "context_window": 4096,
            "cost": {"prompt": 0.5, "completion": 0.8},
        },
        "codellama-13b-instruct": {
            "endpoint": "codellama-13b-instruct-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.5},
        },
        "codellama-7b-instruct": {
            "endpoint": "codellama-7b-instruct-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.1, "completion": 0.25},
        },
        "mistral-7b-instruct-v0.1": {  # TODO: Ask which version this is
            "endpoint": "mistral-7b-instruct-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.1, "completion": 0.25},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "mixtral-8x7b-instruct-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.5},
        },
    }

    def set_api_key(self, api_key: str) -> None:
        """
        Set the API key for OctoAI.

        :param api_key: The API key to set.
        :type api_key: str
        """
        self.client = Client(token=api_key)

    def complete(  # noqa: WPS211, WPS210
        self,
        model: str,
        messages: List,  # type: ignore
        max_tokens: Optional[int] = 512,
        temperature: Optional[float] = 0.9,
        stream: Optional[bool] = False,
    ) -> Optional[ChatCompletion]:
        """
        Complete a prompt using the OctoAI service.

        :param model: The OctoAI model to use for completion.
        :type model: str
        :param messages: List of messages in the conversation.
        :type messages: List
        :param max_tokens: Maximum number of tokens in the generated completion.
        :type max_tokens: Optional[int]
        :param temperature: Controls the randomness of the generated completion.
        :type temperature: Optional[float]
        :param stream: Whether to stream the response.
        :type stream: Optional[bool]
        :return: OctoAI chat completion response.
        :rtype: Optional[ChatCompletion]

        :raises ValueError: If the specified model is not supported.
        """
        if model not in self.supported_models:
            raise ValueError("Model not supported")

        provider_model_endpoint = self.supported_models[model]["endpoint"]
        try:
            if stream:
                return (
                    OctoAIAsyncGeneratorWrapper(
                        self.client.chat.completions.create(
                            model=provider_model_endpoint,
                            messages=messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            stream=stream,
                        ),
                        model,
                        messages,
                        compute_cost_streaming=self.compute_cost_streaming,
                    ),
                    None,
                )
            start_time = datetime.datetime.now()
            response = self.client.chat.completions.create(
                model=provider_model_endpoint,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            ).dict()
            end_time = datetime.datetime.now()
            usage = response["usage"]
            return ChatCompletionResponse(
                model=model,
                created=response.get("created", None),
                id=response.get("id", None),
                object=response.get("object", None),
                usage=response.get("usage", None),
                choices=response.get("choices", None),
                _response_ms=(end_time - start_time).total_seconds() * 1000,
            ), self.compute_cost(
                model,
                [item["content"] for item in messages],
                ModelResponse(
                    usage=Usage(usage["prompt_tokens"], usage["completion_tokens"]),
                ),
            )
        except openai.APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None


class OctoAIAsyncGeneratorWrapper(AsyncGeneratorWrapper):
    """A wrapper for the OctoAI async generator."""

    async def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = ""
        try:  # noqa: WPS501
            for part in self._response:
                if part.choices[0].delta.content is None:
                    continue
                part_dict = part.dict()
                part_dict["model"] = f"{self._model}@octoai"
                part_text = part_dict["choices"][0]["delta"]["content"]
                whole += part_text if part_text else ""
                yield part_dict
        finally:
            self.total_cost = self._compute_cost_streaming(
                self._model,
                whole,
                self._messages,
            )
