import logging

import litellm
import openai

logger = logging.getLogger(__name__)


class BaseCompletionProvider:  # noqa: D101
    def __init__(self) -> None:
        self.supported_models = []
        self.model = None

    def set_api_key(self, api_key) -> None:  # noqa: D102
        litellm.api_key = api_key

    def complete(self, model, messages, max_tokens, temperature) -> str:  # noqa: D102
        if model not in self.supported_models:
            raise ValueError("Model not supported")

        if model is None:
            model = self.model

        try:
            return litellm.completion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except openai.APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
