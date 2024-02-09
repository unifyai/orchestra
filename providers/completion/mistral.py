from providers.completion.base_completion_provider import BaseCompletionProvider


class Mistral(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://docs.mistral.ai/platform/endpoints
    Pricing is per million tokens: https://docs.mistral.ai/platform/pricing
    """

    supported_models = {
        "mistral-7b-instruct-v0.2": {
            "endpoint": "mistral/mistral-tiny",
            "context_window": 32768,
            "cost": {"prompt": 0.14, "completion": 0.42, "currency": "EUR"},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "mistral/mistral-small",
            "context_window": 32768,
            "cost": {"prompt": 0.6, "completion": 1.8, "currency": "EUR"},
        },
        "mistral-medium": {
            "endpoint": "mistral/mistral-medium",
            "context_window": 32768,
            "cost": {"prompt": 2.5, "completion": 7.5, "currency": "EUR"},
        },
    }
