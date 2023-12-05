from providers.completion.base_completion_provider import BaseCompletionProvider


class Anyscale(BaseCompletionProvider):
    """
    A completion provider that uses the Anyscale service.

    Source: https://docs.anyscale.com/endpoints/overview#supported-models
    """

    supported_models = {
        "llama-2-7b-chat-hf": "anyscale/meta-llama/Llama-2-7b-chat-hf",
        "llama-2-13b-chat-hf": "anyscale/meta-llama/Llama-2-13b-chat-hf",
        "llama-2-70b-chat-hf": "anyscale/meta-llama/Llama-2-70b-chat-hf",
        "mistral-7b-instruct-v0.1": "anyscale/mistralai/Mistral-7B-Instruct-v0.1",
        "codellama-34b-instruct-hf": "anyscale/codellama/CodeLlama-34b-Instruct-hf",
    }
