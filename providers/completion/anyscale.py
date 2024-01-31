from providers.completion.base_completion_provider import BaseCompletionProvider


class Anyscale(BaseCompletionProvider):
    """
    A completion provider that uses the Anyscale service.

    Source: https://docs.anyscale.com/endpoints/overview#supported-models
    Pricing is per million tokens: https://docs.endpoints.anyscale.com/pricing
    """

    supported_models = {
        "llama-2-7b-chat": {
            "endpoint": "anyscale/meta-llama/Llama-2-7b-chat-hf",
            "context_window": 4096,
            "cost": {"prompt": 0.15, "completion": 0.15},
        },
        "llama-2-13b-chat": {
            "endpoint": "anyscale/meta-llama/Llama-2-13b-chat-hf",
            "context_window": 4096,
            "cost": {"prompt": 0.25, "completion": 0.25},
        },
        "llama-2-70b-chat": {
            "endpoint": "anyscale/meta-llama/Llama-2-70b-chat-hf",
            "context_window": 4096,
            "cost": {"prompt": 1, "completion": 1},
        },
        "mistral-7b-instruct-v0.1": {
            "endpoint": "anyscale/mistralai/Mistral-7B-Instruct-v0.1",
            "context_window": 16384,
            "cost": {"prompt": 0.15, "completion": 0.15},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "anyscale/mistralai/Mixtral-8x7B-Instruct-v0.1",
            "context_window": 32768,
            "cost": {"prompt": 0.50, "completion": 0.50},
        },
        "codellama-34b-instruct": {
            "endpoint": "anyscale/codellama/CodeLlama-34b-Instruct-hf",
            "context_window": 16384,
            "cost": {"prompt": 1, "completion": 1},
        },
        "zephyr-7b-beta": {
            "endpoint": "anyscale/HuggingFaceH4/zephyr-7b-beta",
            "context_window": 16384,
            "cost": {"prompt": 0.15, "completion": 0.15},
        },
    }
