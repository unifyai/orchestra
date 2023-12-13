from providers.completion.base_completion_provider import BaseCompletionProvider


class Perplexity(BaseCompletionProvider):
    """
    A completion provider that uses the Perplexity service.

    Supported models: https://docs.perplexity.ai/docs/model-cards
    Pricing is per million tokens: https://docs.perplexity.ai/docs/pricing
    """

    supported_models = {
        "codellama-34b-instruct": {
            "endpoint": "perplexity/codellama-34b-instruct",
            "context_window": 16384,
            "cost": {"prompt": 0.35, "completion": 1.4},
        },
        "llama-2-70b-chat": {
            "endpoint": "perplexity/llama-2-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.7, "completion": 2.8},
        },
        "mistral-7b-instruct": {
            "endpoint": "perplexity/mistral-7b-instruct",
            "context_window": 4096,
            "cost": {"prompt": 0.07, "completion": 0.28},
        },
        "pplx-7b-chat": {
            "endpoint": "perplexity/pplx-7b-chat",
            "context_window": 8192,
            "cost": {"prompt": 0.07, "completion": 0.28},
        },
        "pplx-70b-chat": {
            "endpoint": "perplexity/pplx-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.7, "completion": 2.8},
        },
        "pplx-7b-online": {
            "endpoint": "perplexity/pplx-7b-online",
            "context_window": 4096,
            "cost": {
                "prompt": 0,
                "completion": 0.28,
                "online": {"charge_per_1000_requests": 5},
            },
        },
        "pplx-70b-online": {
            "endpoint": "perplexity/pplx-70b-online",
            "context_window": 4096,
            "cost": {
                "prompt": 0,
                "completion": 2.8,
                "online": {"charge_per_1000_requests": 5},
            },
        },
    }
