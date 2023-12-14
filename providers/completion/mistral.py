from providers.completion.base_completion_provider import BaseCompletionProvider


class Mistral(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://docs.mistral.ai/platform/endpoints
    """

    supported_models = {
        "mistral-tiny" # Mistral-7B-v0.2,
        "mistral-small"  # Mixtral-8X7B-v0.1
        "mistal-medium",
    }
