from providers.completion.base_completion_provider import BaseCompletionProvider


class Anyscale(BaseCompletionProvider):  # noqa: D101
    def __init__(self) -> None:
        # https://docs.anyscale.com/endpoints/overview#supported-models
        self.supported_models = [
            "anyscale/meta-llama/Llama-2-7b-chat-hf",
            "anyscale/meta-llama/Llama-2-13b-chat-hf",
            "anyscale/meta-llama/Llama-2-70b-chat-hf",
            "anyscale/mistralai/Mistral-7B-Instruct-v0.1",
            "anyscale/codellama/CodeLlama-34b-Instruct-hf",
        ]
