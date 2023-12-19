import logging
from typing import Dict, List, Optional

import openai
from octoai.client import Client
from octoai.chat import ChatCompletion, get_model_list
from providers.completion.base_completion_provider import BaseCompletionProvider

logger = logging.getLogger(__name__)


class OctoAI(BaseCompletionProvider):
    """
    A completion provider that uses the OctoAI service.

    Supported models: https://docs.octoai.cloud/docs/text-generation
    """

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
        print("models : ", get_model_list())
        if model not in get_model_list():
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
