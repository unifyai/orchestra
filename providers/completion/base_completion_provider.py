import logging
from typing import Dict, List, Optional

import litellm
import openai

logger = logging.getLogger(__name__)


class BaseCompletionProvider:
    """Base class for completion providers."""

    supported_models: Dict[str, str] = {}

    def __init__(self) -> None:
        self.model: str = ""

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        litellm.api_key = api_key

    def complete(  # noqa: D102
        self,
        model: str,
        messages: List,  # type: ignore
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        if model not in self.supported_models:
            raise ValueError("Model not supported")

        provider_model_endpoint = self.supported_models[model]

        try:
            return litellm.completion(
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
        return ""
