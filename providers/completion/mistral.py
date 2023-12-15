from providers.completion.base_completion_provider import BaseCompletionProvider
import logging
from typing import List, Optional, Dict

import openai

from mistralai.client import MistralClient
from mistralai.models.chat_completion import (
    ChatCompletionResponse,
    ChatMessage,
)

logger = logging.getLogger(__name__)


class Mistral(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://docs.mistral.ai/platform/endpoints
    """

    supported_models = {
        "mistral-tiny",  # Mistral-7B-v0.2
        "mistral-small",  # Mixtral-8X7B-v0.1
        "mistral-medium",
    }

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        self.client = MistralClient(api_key=api_key)

    def convert_messages(self, messages: List[Dict[str, str]]) -> List[ChatMessage]:
        messages_mistral = []
        for message in messages:
            messages_mistral.append(
                ChatMessage(role=message["role"], content=message["content"])
            )
        return messages_mistral

    def complete(
        self,
        model: str,
        messages: List,  # type: ignore
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Optional[ChatCompletionResponse]:
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
