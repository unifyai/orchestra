from providers.completion.base_completion_provider import BaseCompletionProvider


class TogetherAI(BaseCompletionProvider):
    """
    A completion provider that uses the TogetherAI service.

    Supported models: https://docs.together.ai/docs/inference-models
    """

    supported_models = {
        "llama-2-70b-chat": "together_ai/togethercomputer/llama-2-70b-chat",
        "llama-2-70b": "together_ai/togethercomputer/llama-2-70b",
        "llama-2-7b-32k": "together_ai/togethercomputer/LLaMA-2-7B-32K",
        "llama-2-7b-32k-instruct": "together_ai/togethercomputer/Llama-2-7B-32K-Instruct",  # noqa: E501
        "llama-2-7b": "together_ai/togethercomputer/llama-2-7b",
        "falcon-40b-instruct": "together_ai/togethercomputer/falcon-40b-instruct",
        "falcon-7b-instruct": "together_ai/togethercomputer/falcon-7b-instruct",
        "alpaca-7b": "together_ai/togethercomputer/alpaca-7b",
        "starchat-alpha": "together_ai/HuggingFaceH4/starchat-alpha",
        "codellama-34b": "together_ai/togethercomputer/CodeLlama-34b",
        "codellama-34b-instruct": "together_ai/togethercomputer/CodeLlama-34b-Instruct",
        "codellama-34b-python": "together_ai/togethercomputer/CodeLlama-34b-Python",
        "sqlcoder": "together_ai/defog/sqlcoder",
        "nsql-llama-2-7b": "together_ai/NumbersStation/nsql-llama-2-7B",
        "wizardcoder-15b-v1.0": "together_ai/WizardLM/WizardCoder-15B-V1.0",
        "wizardcoder-python-34b-v1.0": "together_ai/WizardLM/WizardCoder-Python-34B-V1.0",  # noqa: E501
        "nous-hermes-llama2-13b": "together_ai/NousResearch/Nous-Hermes-Llama2-13b",
        "chronos-hermes-13b": "together_ai/Austism/chronos-hermes-13b",
        "solar-0-70b-16bit": "together_ai/upstage/SOLAR-0-70b-16bit",
        "wizardlm-70b-v1.0": "together_ai/WizardLM/WizardLM-70B-V1.0",
        "mistral-7b-v0.1": "together_ai/mistralai/Mistral-7B-v0.1",
        "mistral-7b-instruct-v0.1": "together_ai/mistralai/Mistral-7B-Instruct-v0.1",
    }
