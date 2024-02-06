# flake8: noqa: E501
from providers.completion.base_completion_provider import BaseCompletionProvider


class TogetherAI(BaseCompletionProvider):
    """
    A completion provider that uses the TogetherAI service.

    Supported models: https://docs.together.ai/docs/inference-models
    Pricing is per million tokens: https://www.together.ai/pricing
    """

    supported_models = {
        "yi-34b": {
            "endpoint": "together_ai/zero-one-ai/Yi-34B",
            "context_window": 4096,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "yi-6b": {
            "endpoint": "together_ai/zero-one-ai/Yi-6B",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "yi-34b-chat": {
            "endpoint": "together_ai/zero-one-ai/Yi-34B-Chat",
            "context_window": 4096,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "alpaca-7b": {
            "endpoint": "together_ai/togethercomputer/alpaca-7b",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "chronos-hermes-13b": {
            "endpoint": "together_ai/Austism/chronos-hermes-13b",
            "context_window": 2048,
            "cost": {"prompt": 0.3, "completion": 0.3},
        },
        "codellama-13b": {
            "endpoint": "together_ai/togethercomputer/CodeLlama-13b",
            "context_window": 16384,
            "cost": {"prompt": 0.225, "completion": 0.225},
        },
        "codellama-34b": {
            "endpoint": "together_ai/togethercomputer/CodeLlama-34b",
            "context_window": 16384,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "codellama-7b": {
            "endpoint": "together_ai/togethercomputer/CodeLlama-7b",
            "context_window": 16384,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "codellama-13b-instruct": {
            "endpoint": "together_ai/codellama/CodeLlama-13b-Instruct-hf",
            "context_window": 16384,
            "cost": {"prompt": 0.225, "completion": 0.225},
        },
        "codellama-34b-instruct": {
            "endpoint": "together_ai/codellama/CodeLlama-34b-Instruct-hf",
            "context_window": 16384,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "codellama-70b-instruct": {
            "endpoint": "together_ai/codellama/CodeLlama-70b-Instruct-hf",
            "context_window": 4096,
            "cost": {"prompt": 0.9, "completion": 0.9},
        },
        "codellama-7b-instruct": {
            "endpoint": "together_ai/codellama/CodeLlama-7b-Instruct-hf",
            "context_window": 16384,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "codellama-13b-python": {
            "endpoint": "together_ai/togethercomputer/CodeLlama-13b-Python",
            "context_window": 16384,
            "cost": {"prompt": 0.225, "completion": 0.225},
        },
        "codellama-34b-python": {
            "endpoint": "together_ai/togethercomputer/CodeLlama-34b-Python",
            "context_window": 16384,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "codellama-7b-python": {
            "endpoint": "together_ai/togethercomputer/CodeLlama-7b-Python",
            "context_window": 16384,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "falcon-40b": {
            "endpoint": "together_ai/togethercomputer/falcon-40b",
            "context_window": 2048,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "falcon-7b": {
            "endpoint": "together_ai/togethercomputer/falcon-7b",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "falcon-40b-instruct": {
            "endpoint": "together_ai/togethercomputer/falcon-40b-instruct",
            "context_window": 2048,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "falcon-7b-instruct": {
            "endpoint": "together_ai/togethercomputer/falcon-7b-instruct",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "gpt-jt-6b-v1": {
            "endpoint": "together_ai/togethercomputer/GPT-JT-6B-v1",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "gpt-jt-moderation-6b": {
            "endpoint": "together_ai/togethercomputer/GPT-JT-Moderation-6B",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "gpt-neoxt-chat-base-20b": {
            "endpoint": "together_ai/togethercomputer/GPT-NeoXT-Chat-Base-20B",
            "context_window": 2048,
            "cost": {"prompt": 0.3, "completion": 0.3},
        },
        "llama-2-13b": {
            "endpoint": "together_ai/togethercomputer/llama-2-13b",
            "context_window": 4096,
            "cost": {"prompt": 0.225, "completion": 0.225},
        },
        "llama-2-70b": {
            "endpoint": "together_ai/togethercomputer/llama-2-70b",
            "context_window": 4096,
            "cost": {"prompt": 0.9, "completion": 0.9},
        },
        "llama-2-7b": {
            "endpoint": "together_ai/togethercomputer/llama-2-7b",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "llama-2-13b-chat": {
            "endpoint": "together_ai/togethercomputer/llama-2-13b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.225, "completion": 0.225},
        },
        "llama-2-70b-chat": {
            "endpoint": "together_ai/togethercomputer/llama-2-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.9, "completion": 0.9},
        },
        "llama-2-7b-chat": {
            "endpoint": "together_ai/togethercomputer/llama-2-7b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "llama-2-7b-32k": {
            "endpoint": "together_ai/togethercomputer/LLaMA-2-7B-32K",
            "context_window": 32768,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "llama-2-7b-32k-instruct": {
            "endpoint": "together_ai/togethercomputer/Llama-2-7B-32K-Instruct",
            "context_window": 32768,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "llemma_7b": {
            "endpoint": "together_ai/EleutherAI/llemma_7b",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "mistral-7b-v0.1": {
            "endpoint": "together_ai/mistralai/Mistral-7B-v0.1",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "mistral-7b-instruct-v0.1": {
            "endpoint": "together_ai/mistralai/Mistral-7B-Instruct-v0.1",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "mistral-7b-instruct-v0.2": {
            "endpoint": "together_ai/mistralai/Mistral-7B-Instruct-v0.2",
            "context_window": 32768,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "mixtral-8x7b-v0.1": {
            "endpoint": "together_ai/mistralai/Mixtral-8x7B-v0.1",
            "context_window": 32768,
            "cost": {"prompt": 0.6, "completion": 0.6},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "together_ai/mistralai/Mixtral-8x7B-Instruct-v0.1",
            "context_window": 32768,
            "cost": {"prompt": 0.6, "completion": 0.6},
        },
        "mythomax-l2-13b": {
            "endpoint": "together_ai/Gryphe/MythoMax-L2-13b",
            "context_window": 4096,
            "cost": {"prompt": 0.3, "completion": 0.3},
        },
        "nsql-llama-2-7b": {
            "endpoint": "together_ai/NumbersStation/nsql-llama-2-7B",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "nous-capybara-7b-v1p9": {
            "endpoint": "together_ai/NousResearch/Nous-Capybara-7B-V1p9",
            "context_window": 8192,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "nous-hermes-llama2-70b": {
            "endpoint": "together_ai/NousResearch/Nous-Hermes-Llama2-70b",
            "context_window": 4096,
            "cost": {"prompt": 0.9, "completion": 0.9},
        },
        "nous-hermes-llama-2-7b": {
            "endpoint": "together_ai/NousResearch/Nous-Hermes-llama-2-7b",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "nous-hermes-llama2-13b": {
            "endpoint": "together_ai/NousResearch/Nous-Hermes-Llama2-13b",
            "context_window": 4096,
            "cost": {"prompt": 0.225, "completion": 0.225},
        },
        "openhermes-2-mistral-7b": {
            "endpoint": "together_ai/teknium/OpenHermes-2-Mistral-7B",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "openhermes-2p5-mistral-7b": {
            "endpoint": "together_ai/teknium/OpenHermes-2p5-Mistral-7B",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "mistral-7b-openorca": {
            "endpoint": "together_ai/Open-Orca/Mistral-7B-OpenOrca",
            "context_window": 8192,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "phind-codellama-34b-python-v1": {
            "endpoint": "together_ai/Phind/Phind-CodeLlama-34B-Python-v1",
            "context_window": 16384,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "phind-codellama-34b-v2": {
            "endpoint": "together_ai/Phind/Phind-CodeLlama-34B-v2",
            "context_window": 16384,
            "cost": {"prompt": 0.8, "completion": 0.8},
        },
        "platypus2-70b-instruct": {
            "endpoint": "together_ai/garage-bAInd/Platypus2-70B-instruct",
            "context_window": 4096,
            "cost": {"prompt": 0.9, "completion": 0.9},
        },
        "pythia-chat-base-7b-v0.16": {
            "endpoint": "together_ai/togethercomputer/Pythia-Chat-Base-7B-v0.16",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "qwen-7b": {
            "endpoint": "together_ai/togethercomputer/Qwen-7B",
            "context_window": 8192,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "qwen-7b-chat": {
            "endpoint": "together_ai/togethercomputer/Qwen-7B-Chat",
            "context_window": 8192,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "redpajama-incite-base-3b-v1": {
            "endpoint": "together_ai/togethercomputer/RedPajama-INCITE-Base-3B-v1",
            "context_window": 2048,
            "cost": {"prompt": 0.1, "completion": 0.1},
        },
        "redpajama-incite-7b-base": {
            "endpoint": "together_ai/togethercomputer/RedPajama-INCITE-7B-Base",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "redpajama-incite-chat-3b-v1": {
            "endpoint": "together_ai/togethercomputer/RedPajama-INCITE-Chat-3B-v1",
            "context_window": 2048,
            "cost": {"prompt": 0.1, "completion": 0.1},
        },
        "redpajama-incite-7b-chat": {
            "endpoint": "together_ai/togethercomputer/RedPajama-INCITE-7B-Chat",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "redpajama-incite-instruct-3b-v1": {
            "endpoint": "together_ai/togethercomputer/RedPajama-INCITE-Instruct-3B-v1",
            "context_window": 2048,
            "cost": {"prompt": 0.1, "completion": 0.1},
        },
        "redpajama-incite-7b-instruct": {
            "endpoint": "together_ai/togethercomputer/RedPajama-INCITE-7B-Instruct",
            "context_window": 2048,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "solar-0-70b-16bit": {
            "endpoint": "together_ai/upstage/SOLAR-0-70b-16bit",
            "context_window": 4096,
            "cost": {"prompt": 0.9, "completion": 0.9},
        },
        "vicuna-13b-v1.5": {
            "endpoint": "together_ai/lmsys/vicuna-13b-v1.5",
            "context_window": 4096,
            "cost": {"prompt": 0.3, "completion": 0.3},
        },
        "vicuna-7b-v1.5": {
            "endpoint": "together_ai/lmsys/vicuna-7b-v1.5",
            "context_window": 4096,
            "cost": {"prompt": 0.2, "completion": 0.2},
        },
        "vicuna-13b-v1.5-16k": {
            "endpoint": "together_ai/lmsys/vicuna-13b-v1.5-16k",
            "context_window": 16384,
            "cost": {"prompt": 0.3, "completion": 0.3},
        },
        "wizardcoder-15b-v1.0": {
            "endpoint": "together_ai/WizardLM/WizardCoder-15B-V1.0",
            "context_window": 8192,
            "cost": {"prompt": 0.3, "completion": 0.3},
        },
        "wizardlm-70b-v1.0": {
            "endpoint": "together_ai/WizardLM/WizardLM-70B-V1.0",
            "context_window": 4096,
            "cost": {"prompt": 0.9, "completion": 0.9},
        },
        "wizardlm-13b-v1.2": {
            "endpoint": "together_ai/WizardLM/WizardLM-13B-V1.2",
            "context_window": 4096,
            "cost": {"prompt": 0.3, "completion": 0.3},
        },
    }
