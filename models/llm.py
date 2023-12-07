import os
from typing import Any, Dict, List

from providers.completion.anyscale import Anyscale
from providers.completion.perplexity import Perplexity
from providers.completion.togetherai import TogetherAI

PROVIDER_CLASSES = {
    "anyscale": Anyscale,
    "perplexity": Perplexity,
    "togetherai": TogetherAI,
}


class CompletionsModel:
    """Sets up a general CompletionsModel service."""

    def __init__(self, provider: str, model: str) -> None:
        if provider.lower() not in PROVIDER_CLASSES:
            raise Exception("Provider not supported by Unify")  # noqa: WPS454

        if model.lower() not in PROVIDER_CLASSES[provider].supported_models:
            raise Exception(f"Model not supported by {provider}")  # noqa: WPS454

        self.provider_obj = PROVIDER_CLASSES[provider]()
        self.model = model.lower()
        api_key = str(os.getenv(f"ORCHESTRA_{provider.upper()}_API_KEY"))
        if api_key is not None:
            self.set_api_key(api_key)

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        self.provider_obj.set_api_key(api_key)

    def get_completion(  # noqa: D102
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 16,
        temperature: float = 0.9,
    ) -> Dict[str, Any]:
        return self.provider_obj.complete(
            self.model,
            messages,
            max_tokens,
            temperature,
        )
