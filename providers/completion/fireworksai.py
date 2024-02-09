from providers.completion.base_completion_provider import BaseCompletionProvider


class FireworksAI(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://fireworks.ai/models
    Pricing is per million tokens: https://fireworks.ai/pricing
    """

    supported_models = {
        "llama-2-7b": {
            "endpoint": "accounts/fireworks/models/llama-v2-7b",
            "context_window": 4096,
            "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
        },
        "llama-2-13b": {
            "endpoint": "accounts/fireworks/models/llama-v2-13b",
            "context_window": 4096,
            "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
        },
        "llama-2-70b": {
            "endpoint": "accounts/fireworks/models/llama-v2-70b",
            "context_window": 4096,
            "cost": {"prompt": 0.70, "completion": 2.80},  # noqa: WPS339
        },
        "llama-2-7b-chat": {
            "endpoint": "accounts/fireworks/models/llama-v2-7b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
        },
        "llama-2-13b-chat": {
            "endpoint": "accounts/fireworks/models/llama-v2-13b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
        },
        "llama-2-70b-chat": {
            "endpoint": "accounts/fireworks/models/llama-v2-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.70, "completion": 2.80},  # noqa: WPS339
        },
        "mistral-7b-v0.1": {
            "endpoint": "accounts/fireworks/models/mistral-7b",
            "context_window": 16384,
            "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
        },
        "mistral-7b-instruct-v0.1": {
            "endpoint": "accounts/fireworks/models/mistral-7b-instruct-4k",
            "context_window": 16384,
            "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "accounts/fireworks/models/mixtral-8x7b-instruct",
            "context_window": 32768,
            "cost": {"prompt": 0.40, "completion": 1.60},  # noqa: WPS339
        },
        "falcon-7b": {
            "endpoint": "accounts/fireworks/models/falcon-7b",
            "context_window": 2048,
            "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
        },
        "falcon-40b": {
            "endpoint": "accounts/fireworks/models/falcon-40b",
            "context_window": 2048,
            "cost": {"prompt": 0.70, "completion": 2.80},  # noqa: WPS339
        },
    }

    def get_base_url(self, *args):
        """Get the base URL.

        :param args: The arguments.

        :return:
            str: The base URL.
        """
        return "https://api.fireworks.ai/inference/v1"
