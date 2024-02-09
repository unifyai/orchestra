import logging

from providers.completion.base_completion_provider import BaseCompletionProvider

logger = logging.getLogger(__name__)


class OctoAI(BaseCompletionProvider):
    """
    A completion provider that uses the OctoAI service.

    Supported models: https://docs.octoai.cloud/docs/text-generation
    Pricing: https://docs.octoai.cloud/docs/pricing (below are per million tokens)
    """

    supported_models = {
        "llama-2-70b-chat": {
            "endpoint": "llama-2-70b-chat-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.6, "completion": 1.9},
        },
        "llama-2-70b-chat-int4": {
            "endpoint": "llama-2-70b-chat-int4",
            "context_window": 4096,
            "cost": {"prompt": 0.6, "completion": 1.2},
        },
        "llama-2-13b-chat": {
            "endpoint": "llama-2-13b-chat-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.5},
        },
        "codellama-34b-instruct": {
            "endpoint": "codellama-34b-instruct-fp16",
            "context_window": 16384,
            "cost": {"prompt": 0.5, "completion": 1.15},
        },
        "codellama-34b-instruct-int4": {
            "endpoint": "codellama-34b-instruct-int4",
            "context_window": 4096,
            "cost": {"prompt": 0.5, "completion": 0.8},
        },
        "codellama-13b-instruct": {
            "endpoint": "codellama-13b-instruct-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.5},
        },
        "codellama-7b-instruct": {
            "endpoint": "codellama-7b-instruct-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.1, "completion": 0.25},
        },
        "mistral-7b-instruct-v0.1": {  # TODO: Ask which version this is
            "endpoint": "mistral-7b-instruct-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.1, "completion": 0.25},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "mixtral-8x7b-instruct-fp16",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.5},
        },
    }

    def get_base_url(self, *args):
        """Get the base URL.

        :param args: The arguments.

        :return: The base URL.
        """
        return "https://text.octoai.run/v1/"
