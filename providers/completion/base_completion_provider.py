import inspect
import logging
from typing import Any, Dict, List, Optional

import tiktoken
from fastapi import HTTPException
from openai import (
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    OpenAI,
    RateLimitError,
)
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.completion_usage import CompletionUsage

logger = logging.getLogger(__name__)

# Pricing info of providers with pay-per-token model is
# standardized to per million tokens.
PRICING_PER_TOKENS = 1000000


class BaseCompletionProvider:
    """Base class for completion providers."""

    # TODO: Make this a property and enforce definition with NotImplemented
    supported_models: Dict[str, Any] = {}
    base_url: str = ""

    def __init__(self) -> None:
        self.model: str = ""

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        self.api_key = api_key

    def get_cost_max(self, model_name: str) -> float:  # noqa: D102
        if model_name not in self.supported_models:
            raise ValueError("Model not supported")
        return (
            self.supported_models[model_name]["cost"]["completion"]
            * self.supported_models[model_name]["context_window"]
            / PRICING_PER_TOKENS
        )

    @property
    def get_base_url(self) -> str:
        """
        Get the base URL.

        :raises NotImplementedError: This method should be implemented in a subclass.
        """
        raise NotImplementedError("This method should be implemented in a subclass")

    async def compute_cost(
        self,
        model_name: str,
        prompts: List[str],
        response: ChatCompletion,
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
                * response.usage.predict_time
            )
        if cost_data.get("online"):
            prompt_cost += cost_data["online"]["charge_per_1000_requests"] / 1000
        prompt_cost += (
            response.usage.prompt_tokens * cost_data["prompt"] / PRICING_PER_TOKENS
        )
        completion_cost = (
            response.usage.completion_tokens
            * cost_data["completion"]
            / PRICING_PER_TOKENS
        )
        return prompt_cost + completion_cost

    async def compute_cost_streaming(  # noqa: WPS210
        self,
        model: str,
        completions: List[str],
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

            tokens = [encoding.encode(completion) for completion in completions]
            completion_tokens = sum(len(token) for token in tokens)

            response = type(  # noqa: WPS317
                "DummyClass",
                (),
                {
                    "usage": CompletionUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=0,
                    ),
                },
            )()
            return await self.compute_cost(
                model,
                [item["content"] for item in messages],  # noqa: WPS441
                response,
            )
        except Exception:
            return 0

    def complete(  # noqa: D102, WPS211, C901, WPS231, WPS238
        self,
        model: str,
        messages: List,  # type: ignore
        **kwargs: Any,
    ) -> Optional[Any]:
        if model not in self.supported_models:
            raise ValueError("Model not supported")

        if isinstance(self.supported_models, dict):
            provider_model_endpoint = self.supported_models[model]["endpoint"]
        else:
            provider_model_endpoint = model

        stream = kwargs.get("stream", False)
        if stream:
            client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.get_base_url(provider_model_endpoint),
            )
        else:
            client = OpenAI(
                api_key=self.api_key,
                base_url=self.get_base_url(provider_model_endpoint),
            )

        try:  # noqa: WPS225
            response = client.chat.completions.create(
                model=provider_model_endpoint,
                messages=messages,
                **kwargs,
            )
            if stream:
                return (
                    AsyncGeneratorWrapper(
                        response,
                        model,
                        messages,
                        compute_cost_streaming=self.compute_cost_streaming,
                    ),
                    None,
                )

            return response, self.compute_cost(
                model,
                [item["content"] for item in messages],
                response,
            )
        except APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
            raise HTTPException(status_code=408, detail=str(error))  # noqa: WPS432
        except RateLimitError as error:
            logger.error(f"Raised openai.RateLimitError, Error: {error}")
            raise HTTPException(status_code=429, detail=str(error))  # noqa: WPS432
        except BadRequestError as error:
            logger.error(f"Raised openai.BadRequestError, Error: {error}")
            raise HTTPException(status_code=400, detail=str(error))  # noqa: WPS432
        except APIError as error:
            logger.error(f"Raised openai.APIError, Error: {error}")
            raise HTTPException(
                status_code=400,  # noqa: WPS432
                detail=str(error),  # noqa: WPS432
            )

        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None, None


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
        whole = []
        usage = {}
        try:  # noqa: WPS501
            if inspect.iscoroutine(self._response):
                self._response = await self._response
            async for part in self._response:
                usage = part.usage if usage in part else {}
                part_dict = part.model_dump()
                choices = part_dict["choices"]
                if choices:  # noqa: WPS338
                    if choices[0]["delta"]["content"] is None:
                        continue  # noqa: WPS220
                part_text = choices[0]["delta"]["content"] if choices else ""
                index = choices[0]["index"] if choices else 0
                if len(whole) <= index:
                    whole.extend([""] * (index - len(whole) + 1))
                whole[index] += part_text
                yield part_dict
        finally:
            if isinstance(usage, CompletionUsage):
                self.total_cost = self._compute_cost(
                    self._model,
                    [item["content"] for item in self._messages],
                    part,  # noqa: WPS441
                )
            else:
                self.total_cost = self._compute_cost_streaming(
                    self._model,
                    whole,
                    self._messages,
                )
