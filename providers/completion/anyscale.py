import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class Anyscale(BaseCompletionProvider):
    def __init__(self):
        self.supported_models = [
            "anyscale/meta-llama/Llama-2-7b-chat-hf",
            "anyscale/meta-llama/Llama-2-13b-chat-hf",
            "anyscale/meta-llama/Llama-2-70b-chat-hf",
            "anyscale/mistralai/Mistral-7B-Instruct-v0.1",
            "anyscale/codellama/CodeLlama-34b-Instruct-hf"
        ]

    def set_api_key(self, api_key):
        litellm.anyscale_key = api_key
