from providers.completion.anyscale import Anyscale
from providers.completion.perplexity import Perplexity
from providers.completion.together_ai import TogetherAI


class Llama2Chat:
    # flake8: noqa: C901
    def __init__(self, provider: str, model: str) -> None:
        supported_providers = {
            "anyscale": {
                "7b": "anyscale/meta-llama/Llama-2-7b-chat-hf",
                "13b": "anyscale/meta-llama/Llama-2-13b-chat-hf",
                "70b": "anyscale/meta-llama/Llama-2-70b-chat-hf",
            },
            "perplexity": {
                "7b": "perplexity/pplx-7b-chat-alpha",
                "13b": "perplexity/llama-2-13b-chat",
                "70b": "perplexity/llama-2-70b-chat",
            },
            "together_ai": {
                "7b": "together_ai/togethercomputer/llama-2-7b",
                "70b": "together_ai/togethercomputer/llama-2-70b",
                "70b-chat": "together_ai/togethercomputer/llama-2-70b-chat",
            },
        }

        if provider not in supported_providers:
            raise Exception("Unsupported provider")

        if model not in supported_providers[provider]:
            raise Exception("Unsupported model")

        if provider == "anyscale":
            self.provider_obj = Anyscale()
        elif provider == "perplexity":
            self.provider_obj = Perplexity()  # type: ignore
        elif provider == "together_ai":
            self.provider_obj = TogetherAI()  # type: ignore
        else:
            raise Exception("Invalid provider")

        self.provider_obj.model = supported_providers[provider][model]

    def set_api_key(self, api_key: str) -> None:
        self.provider_obj.set_api_key(api_key)

    def get_completion(
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
