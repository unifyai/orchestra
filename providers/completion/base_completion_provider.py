import json
import logging
from typing import Any, Dict, List, Optional

import litellm
from litellm.utils import Usage
import openai
import tiktoken

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
            / 1e6  # noqa: WPS432
            * self.supported_models[model]["context_window"]
        )

    def compute_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """
        Compute the cost of a completion.

        :param model: The model to use for completion.
        :type model: str
        :param prompt_tokens: Number of tokens in the prompt.
        :type prompt_tokens: int
        :param completion_tokens: Number of tokens in the completion.
        :type completion_tokens: int

        :return: The cost of the completion.
        """
        return (
            self.supported_models[model]["cost"]["prompt"] * prompt_tokens
            + self.supported_models[model]["cost"]["completion"] * completion_tokens
        ) / 1e6  # noqa: WPS432

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
        total_prompt = ""
        for item in messages:  # noqa: WPS519
            total_prompt += item["content"]
        encoding = tiktoken.get_encoding("cl100k_base")
        tokens = encoding.encode(total_prompt)
        prompt_tokens = len(tokens)

        tokens = encoding.encode(completions)
        completion_tokens = len(tokens)

        return self.compute_cost(model, prompt_tokens, completion_tokens)

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
                return AsyncGeneratorWrapper(
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
                ), None
            response = litellm.completion(
                model=provider_model_endpoint,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if isinstance(response["usage"], Usage):
                usage = response["usage"].model_dump()
            else:
                usage = response["usage"]

            return response, self.compute_cost(
                model,
                usage["prompt_tokens"],
                usage["completion_tokens"],
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
    def __init__(self, response, model, messages, compute_cost_streaming):
        self._response = response
        self._model = model
        self._messages = messages
        self._compute_cost_streaming = compute_cost_streaming
        self.total_cost = None

    async def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = ""
        try:  # noqa: WPS501
            for part in self._response:
                if isinstance(part["usage"], Usage):
                    usage = part["usage"].model_dump()
                else:
                    usage = part["usage"]

                choices = [choice.model_dump() for choice in part.get("choices", []) if hasattr(choice, 'model_dump')]

                part_dict = {
                    "model": self._model,
                    "created": part.get("created", None),
                    "id": part["id"],
                    "choices": choices,
                    "object": part.get("object", "chat.completion"),
                    "usage": usage,
                }
                part_json = json.dumps(part_dict)
                part_text = choices[0]["delta"]["content"]
                whole += part_text if part_text else ""
                yield part_json
        finally:
            self.total_cost = self._compute_cost_streaming(
                self._model,
                whole,
                self._messages,
            )
