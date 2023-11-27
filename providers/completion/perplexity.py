import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class Perplexity(BaseCompletionProvider):
    def __init__(self):
        self.supported_models = [
            "perplexity/codellama-34b-instruct",
            "perplexity/llama-2-13b-chat",
            "perplexity/llama-2-70b-chat",
            "perplexity/mistral-7b-instruct",
            "perplexity/openhermes-2-mistral-7b",
            "perplexity/openhermes-2.5-mistral-7b",
            "perplexity/pplx-7b-chat-alpha",
            "perplexity/pplx-70b-chat-alpha"
        ]

    def set_api_key(self, api_key):
        litellm.perplexity_key = api_key
