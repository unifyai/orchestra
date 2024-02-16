import logging
import os
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
    def api_key(self) -> None:  # noqa: D102
        return os.getenv(self.api_key_var)

    @property
    def max_cost(self) -> float:  # noqa: D102
        return self.completion_cost * self.context_window / PRICING_PER_TOKENS

    def _modify_output(self, out: Dict, **kwargs) -> Dict:
        return out  # noqa: WPS420

    def compute_cost(
        self,
        prompt_tks,
        output_tks,
    ) -> float:
        """
        Returns a deffered op to compute the cost of a completion.

        TODO: Redo docs

        :return: Pre-loaded fn.
        """

        def deferred_cost():
            prompt_cost = prompt_tks * self.prompt_cost / PRICING_PER_TOKENS
            completion_cost = output_tks * self.completion_cost / PRICING_PER_TOKENS
            return prompt_cost + completion_cost

        return deferred_cost

    def compute_cost_streaming(  # noqa: WPS210
        self,
        completions: List[str],
        messages: List[Dict],
    ) -> float:
        """
        Returns a deffered op to compute the cost of a completion when streaming.

        :param completions: The completed text.
        :type completions: str
        :param messages: List of input prompts.
        :type messages: List[Dict]

        :return: Pre-loaded fn.
        """
        try:

            def deferred_streaming_cost():
                total_prompt = ""
                for item in messages:  # noqa: WPS519
                    total_prompt += item["content"]
                # TODO: We need to standarise this and check for gpt models
                encoding = tiktoken.get_encoding("cl100k_base")
                prompt_tokens = len(encoding.encode(total_prompt))
                tokens = [encoding.encode(completion) for completion in completions]
                completion_tokens = sum(len(token) for token in tokens)
                return self.compute_cost(prompt_tokens, completion_tokens)()

            return deferred_streaming_cost

        except Exception:  # TODO: This need to be scoped down and prob moved inside
            return 0

    def __call__(  # noqa: D102, WPS211, C901, WPS231, WPS238, WPS210
        self,
        messages: List,  # type: ignore
        stream: bool = False,
        **kwargs: Any,
    ) -> Optional[Any]:

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        try:  # noqa: WPS225
            response = client.chat.completions.create(
                model=self.provider_endpoint, messages=messages, stream=stream, **kwargs
            )
            if stream:
                return (SyncGeneratorWrapper(self, response, messages), None)

            # TODO: Maybe remove this dump unless neccesary?
            response_dict = self._modify_output(response.model_dump(), stream=stream)
            return response_dict, self.compute_cost(
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
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
            raise HTTPException(status_code=400, detail=str(error))  # noqa: WPS432

    def __call_async__(  # noqa: D102, WPS211, C901, WPS231, WPS238, WPS210
        self,
        messages: List,  # type: ignore
        stream: bool = False,
        **kwargs: Any,
    ) -> Optional[Any]:

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

        try:  # noqa: WPS225
            response = client.chat.completions.create(
                model=self.provider_endpoint, messages=messages, stream=stream, **kwargs
            )
            if stream:
                return (AsyncGeneratorWrapper(self, response, messages), None)

            # TODO: Maybe remove this dump unless neccesary?
            response_dict = self._modify_output(response.model_dump(), stream=stream)
            return response_dict, self.compute_cost(
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
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
            raise HTTPException(status_code=400, detail=str(error))  # noqa: WPS432


class BaseGeneratorWrapper:
    def __init__(self, provider, response, messages):
        self.provider = provider
        self._response = response
        self._messages = messages
        self.total_cost = None

    def generator_iteration(self, part, whole):
        part_dict = part.model_dump()
        part_dict = self.provider._modify_output(part_dict, stream=True)
        choices = part_dict["choices"]
        if choices:
            if choices[0]["delta"]["content"] is None:
                return None
        part_text = choices[0]["delta"]["content"] if choices else ""
        index = choices[0]["index"] if choices else 0
        if len(whole) <= index:
            whole.extend([""] * (index - len(whole) + 1))
        whole[index] += part_text
        return part_dict


class SyncGeneratorWrapper(BaseGeneratorWrapper):  # noqa: D101
    def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = []
        try:  # noqa: WPS501
            for part in self._response:
                part_dict = self.generator_iteration(part, whole)
                if part_dict is None:
                    continue
                yield part_dict
        finally:
            self.total_cost = self.provider.compute_cost_streaming(
                whole,
                self._messages,
            )


# TODO: Remove code duplication here
class AsyncGeneratorWrapper(BaseGeneratorWrapper):  # noqa: D101
    async def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = []
        try:  # noqa: WPS501
            async for part in await self._response:
                part_dict = self.generator_iteration(part, whole)
                if part_dict is None:
                    continue
                yield part_dict
        finally:
            self.total_cost = self.provider.compute_cost_streaming(
                whole,
                self._messages,
            )
