from providers.completion.base_completion_provider import BaseCompletionProvider


class Deepinfra(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://deepinfra.com/pricing
    Pricing is per million tokens: https://deepinfra.com/pricing
    """

    supported_models = {
        "llama-2-7b-chat": {
            "endpoint": "meta-llama/Llama-2-7b-chat-hf",
            "context_window": 4096,
            "cost": {"prompt": 0.13, "completion": 0.13},
        },
        "llama-2-13b-chat": {
            "endpoint": "meta-llama/Llama-2-13b-chat-hf",
            "context_window": 4096,
            "cost": {"prompt": 0.22, "completion": 0.22},
        },
        "llama-2-70b-chat": {
            "endpoint": "meta-llama/Llama-2-70b-chat-hf",
            "context_window": 4096,
            "cost": {"prompt": 0.70, "completion": 0.90},  # noqa: WPS339
        },
        "mistral-7b-instruct-v0.1": {
            "endpoint": "mistralai/Mistral-7B-Instruct-v0.1",
            "context_window": 32768,
            "cost": {"prompt": 0.13, "completion": 0.13},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "mistralai/Mixtral-8x7B-Instruct-v0.1",
            "context_window": 32768,
            "cost": {"prompt": 0.27, "completion": 0.27},  # noqa: WPS339
        },
    }

    def get_base_url(self, *args):
        """Get the base URL.

        :param args: The arguments.

        :return: The base URL.
        """
        return "https://api.deepinfra.com/v1/openai"
