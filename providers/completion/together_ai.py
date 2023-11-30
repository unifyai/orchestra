from providers.completion.base_completion_provider import BaseCompletionProvider


class TogetherAI(BaseCompletionProvider):  # noqa: D101
    def __init__(self):
        # https://docs.together.ai/docs/inference-models
        self.supported_models = [
            "together_ai/togethercomputer/llama-2-70b-chat",
            "together_ai/togethercomputer/llama-2-70b",
            "together_ai/togethercomputer/LLaMA-2-7B-32K",
            "together_ai/togethercomputer/Llama-2-7B-32K-Instruct",
            "together_ai/togethercomputer/llama-2-7b",
            "together_ai/togethercomputer/falcon-40b-instruct",
            "together_ai/togethercomputer/falcon-7b-instruct",
            "together_ai/togethercomputer/alpaca-7b",
            "together_ai/HuggingFaceH4/starchat-alpha",
            "together_ai/togethercomputer/CodeLlama-34b",
            "together_ai/togethercomputer/CodeLlama-34b-Instruct",
            "together_ai/togethercomputer/CodeLlama-34b-Python",
            "together_ai/defog/sqlcoder",
            "together_ai/NumbersStation/nsql-llama-2-7B",
            "together_ai/WizardLM/WizardCoder-15B-V1.0",
            "together_ai/WizardLM/WizardCoder-Python-34B-V1.0",
            "together_ai/NousResearch/Nous-Hermes-Llama2-13b",
            "together_ai/Austism/chronos-hermes-13b",
            "together_ai/upstage/SOLAR-0-70b-16bit",
            "together_ai/WizardLM/WizardLM-70B-V1.0",
            "together_ai/mistralai/Mistral-7B-Instruct-v0.1"
            "together_ai/mistralai/Mistral-7B-v0.1",
        ]
