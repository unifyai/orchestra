from providers.completion.anyscale import Anyscale
from providers.completion.replicate import Replicate
from providers.completion.perplexity import Perplexity
from providers.completion.together_ai import TogetherAI


class Llama2Chat:
    def __init__(self, provider, model):
        supported_providers = {
            "anyscale": {
                "7b": "anyscale/meta-llama/Llama-2-7b-chat-hf",
                "13b": "anyscale/meta-llama/Llama-2-13b-chat-hf",
                "70b": "anyscale/meta-llama/Llama-2-70b-chat-hf"
            },
            "perplexity": {
                "7b": "perplexity/pplx-7b-chat-alpha",
                "13b": "perplexity/llama-2-13b-chat",
                "70b": "perplexity/llama-2-70b-chat"
            },
            "replicate": {
                "7b": "replicate/llama-2-7b-chat",
                "13b": "replicate/a16z-infra/llama-2-13b-chat",
                "70b": "replicate/llama-2-70b-chat"
            },
            "together_ai": {
                "7b": "together_ai/togethercomputer/llama-2-7b",
                "70b": "together_ai/togethercomputer/llama-2-70b"
            }
        }

        if provider not in supported_providers:
            raise Exception("Unsupported provider")

        if model not in supported_providers[provider]:
            raise Exception("Unsupported model")

        if provider == "anyscale":
            self.provider_obj = Anyscale()
        elif provider == "perplexity":
            self.provider_obj = Perplexity()
        elif provider == "replicate":
            self.provider_obj = Replicate()
        elif provider == "together_ai":
            self.provider_obj = TogetherAI()
        else:
            raise Exception("Invalid provider")

        self.provider_obj.model = supported_providers[provider][model]

    def set_api_key(self, api_key):
        self.provider_obj.set_api_key(api_key)

    def get_completion(self, prompt, max_tokens=16, temperature=0.9):
        messages = [{"content": prompt, "role": "user"}]
        return self.provider_obj.complete(self.provider_obj.model, messages, max_tokens, temperature)
