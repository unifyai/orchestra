from providers.completion.anyscale import Anyscale
from providers.completion.perplexity import Perplexity
from providers.completion.together_ai import TogetherAI

PROVIDER_CLASSES = {
    "anyscale": Anyscale,
    "perplexity": Perplexity,
    "together_ai": TogetherAI,
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

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        self.provider_obj.set_api_key(api_key)

    def get_completion(  # noqa: D102
        self,
        prompt: str,
        max_tokens: int = 16,
        temperature: float = 0.9,
    ) -> str:
        messages = [{"content": prompt, "role": "user"}]
        return self.provider_obj.complete(
            self.model,
            messages,
            max_tokens,
            temperature,
        )
