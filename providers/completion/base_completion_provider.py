import os
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

    def __init__(self, hub_model) -> None:
        self.hub_model: str = hub_model
        self.supported_models: Dict[str, Any] = {}

    @property
    def api_key_var(self) -> str:
        """
        Get the provider api key var NAME.

        :raises NotImplementedError: This method should be implemented in a subclass.
        """
        raise NotImplementedError("This method should be implemented in a subclass")

    @property
    def base_url(self) -> str:
        """
        Get the base URL.

        :raises NotImplementedError: This method should be implemented in a subclass.
        """
        raise NotImplementedError("This method should be implemented in a subclass")

    @property
    def provider_endpoint(self):
        # TODO: Docs
        # TODO: Add logic to raise an error if self.supported_models is empty
        return self.supported_models[self.hub_model]["endpoint"]

    @property
    def prompt_cost(self):
        # TODO: Docs
        # TODO: Add logic to raise an error if self.supported_models is empty
        return self.supported_models[self.hub_model]["cost"]["prompt"]

    @property
    def completion_cost(self):
        # TODO: Docs
        # TODO: Add logic to raise an error if self.supported_models is empty
        return self.supported_models[self.hub_model]["cost"]["completion"]

    @property
    def context_window(self):
        # TODO: Docs
        # TODO: Add logic to raise an error if self.supported_models is empty
        return self.supported_models[self.hub_model]["context_window"]

    @property
    def get_base_url(self) -> str:
        """
        Get the base URL.

        :raises NotImplementedError: This method should be implemented in a subclass.
        """
        raise NotImplementedError("This method should be implemented in a subclass")

    @property
    def api_key(self) -> None:  # noqa: D102
        return os.getenv(self.api_key_var)

    @property
    def max_cost(self) -> float:  # noqa: D102
        return self.completion_cost * self.context_window / PRICING_PER_TOKENS

    async def compute_cost(
        self,
        prompt_tks,
        output_tks,
    ) -> float:
        """
        Compute the cost of a completion.

        TODO: Redo docs

        :return: The cost of the completion.
        """
        prompt_cost = prompt_tks * self.prompt_cost / PRICING_PER_TOKENS
        completion_cost = output_tks * self.completion_cost / PRICING_PER_TOKENS
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
            # TODO: We need to standarise this and check for gpt models
            encoding = tiktoken.get_encoding("cl100k_base")
            prompt_tokens = len(encoding.encode(total_prompt))
            tokens = [encoding.encode(completion) for completion in completions]
            completion_tokens = sum(len(token) for token in tokens)
            return await self.compute_cost(prompt_tokens, completion_tokens)

        except Exception:  # TODO: This need to be scoped down
            return 0

    def __call__(  # noqa: D102, WPS211, C901, WPS231, WPS238, WPS210
        self,
        messages: List,  # type: ignore
        **kwargs: Any,
    ) -> Optional[Any]:

        stream = kwargs.get("stream", False)
        if stream:
            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        else:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        try:  # noqa: WPS225
            response = client.chat.completions.create(
                model=self.provider_endpoint,
                messages=messages,
                **kwargs,
            )
            if stream:
                return (
                    AsyncGeneratorWrapper(
                        self,
                        response,
                        self.hub_model,  # TODO: USe this directly
                        messages,
                        compute_cost_streaming=self.compute_cost_streaming,
                    ),
                    None,
                )

            # TODO: Maybe remove this dump unless neccesary?
            response_dict = self._modify_output(response.model_dump())
            return response_dict, self.compute_cost(
                response.usage.prompt_tokens, response.usage.completion_tokens
            )
        # TODO: These needs to be processed correctly in our endpoint
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
        # except Exception as error:
        #     error_type = type(error)
        #     logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None, None

    def _modify_output(self, out: Dict, **kwargs) -> Dict:
        return out  # noqa: WPS420


class AsyncGeneratorWrapper:  # noqa: D101
    def __init__(  # noqa: WPS211
        self,
        base_provider,
        response,
        model,
        messages,
        compute_cost_streaming,
        compute_cost=None,  # noqa: WPS211, E501
    ):
        self.base_provider = base_provider
        self._response = response
        self._model = model
        self._messages = messages
        self._compute_cost_streaming = compute_cost_streaming
        self._compute_cost = compute_cost
        self.total_cost = None

    async def generator(self):  # noqa: D102, C901, WPS210, WPS231
        # TODO: Is this being used at all?
        whole = []
        usage = {}
        try:  # noqa: WPS501
            if inspect.iscoroutine(self._response):
                self._response = await self._response
            async for part in self._response:
                usage = part.usage if usage in part else {}
                part_dict = part.model_dump()
                self.base_provider._modify_output(part_dict)
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
