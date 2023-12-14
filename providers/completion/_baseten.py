from providers.completion.base_completion_provider import BaseCompletionProvider


class Baseten(BaseCompletionProvider):
    """
    A completion provider that uses the Baseten service.

    Initializes with list of few OSS models as example since hourly payment.
    """

    supported_models = {
        "falcon-7b": {
            "endpoint": "baseten/qvv0xeq",
            "context_window": 2048,
            "cost": {"hardware": "a40", "per_hour": True},
        },
        "wizardlm-70b": {
            "endpoint": "baseten/q841o8w",
            "context_window": 4096,
            "cost": {"hardware": "a40", "per_hour": True},
        },
        "mpt-7b": {
            "endpoint": "baseten/31dxrj3",
            "context_window": 2048,
            "cost": {"hardware": "a40", "per_hour": True},
        },
    }
