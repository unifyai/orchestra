from providers.completion.base_completion_provider import BaseCompletionProvider


class Replicate(BaseCompletionProvider):
    """
    Initializes with list of few OSS models as example.

    Source: https://replicate.com/explore
    """

    supported_models = {
        "mistral-7b-instruct-v0.1": "replicate/mistralai/mistral-7b-instruct-v0.1",
        "llama-2-70b-chat": "replicate/meta/llama-2-70b-chat",
        "gpt-j-6b": "replicate/gpt-j-6b",
    }
