from providers.completion.base_completion_provider import BaseCompletionProvider


class Perplexity(BaseCompletionProvider):
    """
    A completion provider that uses the Perplexity service.

    Supported models: https://docs.perplexity.ai/docs/model-cards
    Pricing is per million tokens: https://docs.perplexity.ai/docs/pricing
    """

    supported_models = {
        "codellama-34b-instruct": {
            "endpoint": "codellama-34b-instruct",
            "context_window": 16384,
            "cost": {"prompt": 0.35, "completion": 1.4},
        },
        "llama-2-70b-chat": {
            "endpoint": "llama-2-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.7, "completion": 2.8},
        },
        "mistral-7b-instruct-v0.2": {
            "endpoint": "mistral-7b-instruct",
            "context_window": 4096,
            "cost": {"prompt": 0.07, "completion": 0.28},
        },
        # "mixtral-8x7b-instruct-v0.1": {
        #     "endpoint": "mixtral-8x7b-instruct",
        #     "context_window": 4096,
        #     "cost": {"prompt": 0.13, "completion": 0.56}, This needs to be revisited
        # },
        "pplx-7b-chat": {
            "endpoint": "pplx-7b-chat",
            "context_window": 8192,
            "cost": {"prompt": 0.07, "completion": 0.28},
        },
        "pplx-70b-chat": {
            "endpoint": "pplx-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.7, "completion": 2.8},
        },
        "pplx-7b-online": {
            "endpoint": "pplx-7b-online",
            "context_window": 4096,
            "cost": {
                "prompt": 0,
                "completion": 0.28,
                "online": {"charge_per_1000_requests": 5},
            },
        },
        "pplx-70b-online": {
            "endpoint": "pplx-70b-online",
            "context_window": 4096,
            "cost": {
                "prompt": 0,
                "completion": 2.8,
                "online": {"charge_per_1000_requests": 5},
            },
        },
    }

    def get_base_url(self, *args):
        """Get the base URL.

        :param args: The arguments.

        :return: The base URL.
        """
        return "https://api.perplexity.ai/"
