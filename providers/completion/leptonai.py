from providers.completion.base_completion_provider import BaseCompletionProvider


class LeptonAI(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://www.lepton.ai/playground
    Pricing is per million tokens: https://www.lepton.ai/pricing
    """

    supported_models = {
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "mixtral-8x7b",
            "context_window": 32768,
            "cost": {"prompt": 0.5, "completion": 0.5},
        },
        "llama-2-7b-chat": {
            "endpoint": "llama2-7b",
            "context_window": 4096,
            "cost": {"prompt": 0.1, "completion": 0.1},
        },
        "llama-2-13b-chat": {
            "endpoint": "llama2-13b",
            "context_window": 4096,
            "cost": {"prompt": 0.3, "completion": 0.3},
        },
        "llama-2-70b-chat": {
            "endpoint": "llama2-70b",
            "context_window": 4096,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
    }

    def get_base_url(self, endpoint):
        return "https://{}.lepton.run/api/v1/".format(endpoint)
