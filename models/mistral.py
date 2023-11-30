from providers.completion.anyscale import Anyscale
from providers.completion.perplexity import Perplexity
from providers.completion.together_ai import TogetherAI


class Mistral:
    """Sets up Mistral service."""

    SUPPORTED_PROVIDERS = {
        "anyscale": {
            "7b-instruct": "anyscale/mistralai/Mistral-7B-Instruct-v0.1",
        },
        "perplexity": {
            "7b-instruct": "perplexity/mistral-7b-instruct",
        },
        "together_ai": {
            "7b": "together_ai/mistralai/Mistral-7B-v0.1",
            "7b-instruct": "together_ai/mistralai/Mistral-7B-Instruct-v0.1",
        },
    }

    PROVIDER_CLASSES = {
        "anyscale": Anyscale,
        "perplexity": Perplexity,
        "together_ai": TogetherAI,
    }

    def __init__(self, provider: str, model: str) -> None:
        if self.SUPPORTED_PROVIDERS.keys() != self.PROVIDER_CLASSES.keys():
            raise Exception(  # noqa: WPS454
                "Ensure all providers classes are supported",
            )
        if provider not in self.SUPPORTED_PROVIDERS:
            raise Exception("Unsupported provider")  # noqa: WPS454

        if model not in self.SUPPORTED_PROVIDERS[provider]:
            raise Exception("Unsupported model")  # noqa: WPS454

        self.provider_obj = self.PROVIDER_CLASSES[provider]()
        self.provider_obj.model = self.SUPPORTED_PROVIDERS[provider][model]

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
            self.provider_obj.model,
            messages,
            max_tokens,
            temperature,
        )
