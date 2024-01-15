# flake8: noqa: E501

from providers.completion.base_completion_provider import BaseCompletionProvider


class Replicate(BaseCompletionProvider):
    """
    Initializes with list of few OSS models as example.

    Source: https://replicate.com/explore
    Pricing has dual pricing: either pay for time it takes to process your request or
    per million tokens: https://replicate.com/pricing
    """

    hardware_pricing_per_sec = {
        "cpu": 0.000100,
        "t4": 0.000225,
        "a40": 0.000575,
        "a40-large": 0.000725,
        "a100-40gb": 0.001150,
        "a100-80gb": 0.001400,
        "8xa40": 0.005800,
    }
    supported_models = {
        "mistral-7b-instruct-v0.1": {
            "endpoint": "replicate/mistralai/mistral-7b-instruct-v0.1:83b6a56e7c828e667f21fd596c338fd4f0039b46bcfa18d973e8e70e455fda70",
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "mistral-7b-instruct-v0.2": {
            "endpoint": "replicate/mistralai/mistral-7b-instruct-v0.2",
            "context_window": 16384,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "replicate/mistralai/mixtral-8x7b-instruct-v0.1",
            "context_window": 16384,
            "cost": {"prompt": 0.30, "completion": 1.00},
        },
        "mistral-7b-v0.1": {
            "endpoint": "replicate/mistralai/mistral-7b-v0.1",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "llama-2-70b": {
            "endpoint": "replicate/meta/llama-2-70b",
            "context_window": 4096,
            "cost": {"prompt": 0.65, "completion": 2.75},
        },
        "llama-2-70b-chat": {
            "endpoint": "replicate/meta/llama-2-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.65, "completion": 2.75},
        },
        "gpt-j-6b": {
            "endpoint": "replicate/gpt-j-6b:b3546aeec6c9891f0dd9929c2d3bedbf013c12e02e7dd0346af09c37e008c827",
            "context_window": 2048,
            "cost": {"hardware": "a100-40gb", "per_second": True},
        },
        "llama-2-13b": {
            "endpoint": "replicate/meta/llama-2-13b",
            "context_window": 4096,
            "cost": {"prompt": 0.10, "completion": 0.50},
        },
        "llama-2-13b-chat": {
            "endpoint": "replicate/meta/llama-2-13b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.10, "completion": 0.50},
        },
        "llama-2-7b": {
            "endpoint": "replicate/meta/llama-2-7b",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "llama-2-7b-chat": {
            "endpoint": "replicate/meta/llama-2-7b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
    }

    def get_cost_max(self, model_name: str) -> float:  # noqa: D102
        if model_name not in self.supported_models:
            raise ValueError("Model not supported")
        cost_data = self.supported_models[model_name]["cost"]
        # Defined constant used to approximate maximum cost.
        # Represents the maximum time a server might take to process a request.
        max_runtime_secs = 100
        return self.hardware_pricing_per_sec[cost_data["hardware"]] * max_runtime_secs
