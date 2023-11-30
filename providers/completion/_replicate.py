from providers.completion.base_completion_provider import BaseCompletionProvider


class Replicate(BaseCompletionProvider):
    """
    Initializes with list of few OSS models as example.

    Source: https://replicate.com/explore
    """

    def __init__(self) -> None:
        self.supported_models = [
            "replicate/mistralai/mistral-7b-instruct-v0.1",
            "replicate/meta/llama-2-70b-chat",
            "replicate/gpt-j-6b",
        ]
