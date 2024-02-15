from providers.completion.base_completion_provider import BaseCompletionProvider


class Deepinfra(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://deepinfra.com/pricing
    Pricing is per million tokens: https://deepinfra.com/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_DEEPINFRA_API_KEY"

    @property
    def base_url(self):
        return "https://api.deepinfra.com/v1/openai"


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
    "codellama-34b-instruct": {
        "endpoint": "codellama/CodeLlama-34b-Instruct-hf",
        "context_window": 16384,
        "cost": {"prompt": 0.60, "completion": 0.60},  # noqa: WPS339
    },
    "phind-codellama-34b-v2": {
        "endpoint": "Phind/Phind-CodeLlama-34B-v2",
        "context_window": 16384,
        "cost": {"prompt": 0.60, "completion": 0.60},  # noqa: WPS339
    },
    "mythomax-l2-13b": {
        "endpoint": "Gryphe/MythoMax-L2-13b",
        "context_window": 4096,
        "cost": {"prompt": 0.22, "completion": 0.22},
    },
    "yi-34b-chat": {
        "endpoint": "01-ai/Yi-34B-Chat",
        "context_window": 4096,
        "cost": {"prompt": 0.60, "completion": 0.60},  # noqa: WPS339
    },
}
