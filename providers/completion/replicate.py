import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class Replicate(BaseCompletionProvider):
    def __init__(self):
        self.supported_models = [
            "replicate/llama-2-70b-chat",
            "replicate/a16z-infra/llama-2-13b-chat",
            "replicate/vicuna-13b",
            "replicate/daanelson/flan-t5-large",
            "replicate/custom-llm-version-id",
            "replicate/deployments/ishaan-jaff/ishaan-mistral"
        ]

    def set_api_key(self, api_key):
        litellm.replicate_key = api_key
