import logging
from typing import List, Optional

import openai
from octoai.chat import ChatCompletion
from octoai.client import Client
from providers.completion.base_completion_provider import BaseCompletionProvider

logger = logging.getLogger(__name__)


class OctoAI(BaseCompletionProvider):
    """
    A completion provider that uses the OctoAI service.

    Supported models: https://docs.octoai.cloud/docs/text-generation
    Pricing: https://docs.octoai.cloud/docs/pricing (below are per million tokens)
    """

    supported_models = {
        "Llama2-70B-FP16": {
            "endpoint": "Llama2-70B-FP16",
            "context_window": 4096,
            "cost": {"prompt": 0.6, "completion": 1.9},
        },
        "Llama2-70B-INT4": {
            "endpoint": "Llama2-70B-INT4",
            "context_window": 4096,
            "cost": {"prompt": 0.6, "completion": 1.2},
        },
        "Llama2-13B-FP16": {
            "endpoint": "Llama2-13B-FP16",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.5},
        },
        "CodeLlama-34B-FP16": {
            "endpoint": "CodeLlama-34B-FP16",
            "context_window": 16384,
            "cost": {"prompt": 0.5, "completion": 1.15},
        },
        "CodeLlama-34B-INT4": {
            "endpoint": "CodeLlama-34B-INT4",
            "context_window": 4096,
            "cost": {"prompt": 0.5, "completion": 0.8},
        },
        "CodeLlama-13B-FP16": {
            "endpoint": "CodeLlama-13B-FP16",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.5},
        },
        "CodeLlama-7B-FP16": {
            "endpoint": "CodeLlama-7B-FP16",
            "context_window": 4096,
            "cost": {"prompt": 0.1, "completion": 0.25},
        },
        "Mistral-7B-FP16": {
            "endpoint": "Mistral-7B-FP16",
            "context_window": 4096,
            "cost": {"prompt": 0.1, "completion": 0.25},
        },
    }

    def set_api_key(self, api_key: str) -> None:
        """
        Set the API key for OctoAI.

        :param api_key: The API key to set.
        :type api_key: str
        """
        self.client = Client(token=api_key)

    def complete(
        self,
        model: str,
        messages: List,  # type: ignore
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
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
        :return: OctoAI chat completion response.
        :rtype: Optional[ChatCompletion]

        :raises ValueError: If the specified model is not supported.
        """
        if model not in self.supported_models:
            raise ValueError("Model not supported")

        provider_model_endpoint = model
        try:
            return self.client.chat.completions.create(
                model=provider_model_endpoint,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except openai.APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None
