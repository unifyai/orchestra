import logging
from typing import Dict, List, Optional

import openai
from mistralai.client import MistralClient
from mistralai.models.chat_completion import ChatCompletionResponse, ChatMessage
from providers.completion.base_completion_provider import BaseCompletionProvider

logger = logging.getLogger(__name__)


class Mistral(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://docs.mistral.ai/platform/endpoints
    Pricing is per million tokens: https://docs.mistral.ai/platform/pricing
    """

    supported_models = {
        "mistral-tiny": {
            "endpoint": "mistral-tiny",
            "context_window": 32768,
            "cost": {"prompt": 0.15, "completion": 0.46},
        },
        "mistral-small": {
            "endpoint": "mistral-small",
            "context_window": 32768,
            "cost": {"prompt": 0.66, "completion": 1.97},
        },
        "mistral-medium": {
            "endpoint": "mistral-medium",
            "context_window": 32768,
            "cost": {"prompt": 2.74, "completion": 8.21},
        },
    }

    def set_api_key(self, api_key: str) -> None:
        """
        Set the API key for Mistral.

        :param api_key: The API key to set.
        :type api_key: str
        """
        self.client = MistralClient(api_key=api_key)

    def convert_messages(self, messages: List[Dict[str, str]]) -> List[ChatMessage]:
        """
        Convert a list of messages to Mistral's chat message format.

        :param messages: List of messages with "role" and "content" keys.
        :type messages: List[Dict[str, str]]
        :return: List of ChatMessage objects.
        :rtype: List[ChatMessage]
        """
        messages_mistral = []
        for message in messages:
            messages_mistral.append(
                ChatMessage(role=message["role"], content=message["content"]),
            )
        return messages_mistral

    def complete(
        self,
        model: str,
        messages: List,  # type: ignore
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Optional[ChatCompletionResponse]:
        """
        Complete a prompt using the Mistral service.

        :param model: The Mistral model to use for completion.
        :type model: str
        :param messages: List of messages in the conversation.
        :type messages: List
        :param max_tokens: Maximum number of tokens in the generated completion.
        :type max_tokens: Optional[int]
        :param temperature: Controls the randomness of the generated completion.
        :type temperature: Optional[float]
        :return: Mistral chat completion response.
        :rtype: Optional[ChatCompletionResponse]

        :raises ValueError: If the specified model is not supported.
        """
        if model not in self.supported_models:
            raise ValueError("Model not supported")

        if isinstance(self.supported_models, dict):
            provider_model_endpoint = self.supported_models[model]
        else:
            provider_model_endpoint = model
        messages = self.convert_messages(messages)
        try:
            return self.client.chat(
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
