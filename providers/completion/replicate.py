# flake8: noqa: E501
from typing import List, Optional

from litellm.utils import ModelResponse
from providers.completion.base_completion_provider import BaseCompletionProvider


class Replicate(BaseCompletionProvider):
    """
    Initializes with list of few OSS models as example.

    Source: https://replicate.com/explore
    Pricing is pay for time it takes to process your request: https://replicate.com/pricing
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
        "mistral-7b-v0.1": {
            "endpoint": "replicate/mistralai/mistral-7b-v0.1:3e8a0fb6d7812ce30701ba597e5080689bef8a013e5c6a724fafb108cc2426a0",
            "context_window": 4096,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "llama-2-70b-chat": {
            "endpoint": "replicate/meta/llama-2-70b-chat:02e509c789964a7ea8736978a43525956ef40397be9033abf9fd2badfe68c9e3",
            "context_window": 4096,
            "cost": {"hardware": "a100-80gb", "per_second": True},
        },
        "gpt-j-6b": {
            "endpoint": "replicate/gpt-j-6b:b3546aeec6c9891f0dd9929c2d3bedbf013c12e02e7dd0346af09c37e008c827",
            "context_window": 2048,
            "cost": {"hardware": "a100-40gb", "per_second": True},
        },
        "llama-2-13b-chat": {
            "endpoint": "replicate/meta/llama-2-13b-chat:f4e2de70d66816a838a89eeeb621910adffb0dd0baba3976c96980970978018d",
            "context_window": 4096,
            "cost": {"hardware": "a40-large", "per_second": True},
        },
        "llama-2-7b-chat": {
            "endpoint": "replicate/meta/llama-2-7b-chat:13c3cdee13ee059ab779f0291d29054dab00a47dad8261375654de5540165fb0",
            "context_window": 4096,
            "cost": {"hardware": "a40-large", "per_second": True},
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

    def compute_cost(
        self,
        model_name: str,
        prompts: Optional[List[str]],
        response: ModelResponse,
    ) -> float:
        """
        Compute the cost of a completion.

        :param model_name: The model to use for completion.
        :param prompts: List of the prompt texts.
        :param response: Model response from LiteLLM completion.

        :return: The cost of the completion.
        """
        cost_data = self.supported_models[model_name]["cost"]  # type: ignore
        total_cost = (
            self.hardware_pricing_per_sec[cost_data["hardware"]]  # type: ignore
            * response._response_ms
            / 1000
        )
        return total_cost
