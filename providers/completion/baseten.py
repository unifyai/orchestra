import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class Baseten(BaseCompletionProvider):
    def __init__(self):
        self.supported_models = [
            "baseten/qvv0xeq",
            "baseten/q841o8w",
            "baseten/31dxrj3"
        ]

    def set_api_key(self, api_key):
        litellm.baseten_key = api_key
