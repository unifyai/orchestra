import os 
import litellm
import openai

class BaseCompletionService:
    def __init__(self):
        self.supported_models = []

    def complete(self, model, messages, max_tokens, temperature):
        if model not in self.supported_models:
            raise Exception("Model not supported")

        try:
            response = litellm.completion(
                model = model,
                messages = messages,
                max_tokens = max_tokens,
                temperature = temperature
            )
            return response
        except openai.APITimeoutError as e:
            print(f"Raised error type: {type(e)}, Error: {e}")
            pass
        except Exception as e:
            print(f"Raised error type: {type(e)}, Error: {e}")
            pass


class OpenAI(BaseCompletionService):
    def __init__(self, api_key, openai_organization=None, openai_api_base=None):
        litellm.openai_key = api_key
        if openai_organization:
            litellm.organization = openai_organization
        if openai_api_base:
            litellm.api_version = openai_api_base

        self.supported_models = [
            "gpt-4-1106-preview",
            "gpt-3.5-turbo",
            "gpt-3.5-turbo-0301",
            "gpt-3.5-turbo-0613",
            "gpt-3.5-turbo-16k",
            "gpt-3.5-turbo-16k-0613",
            "gpt-4",
            "gpt-4-0314",
            "gpt-4-0613",
            "gpt-4-32k",
            "gpt-4-32k-0314",
            "gpt-4-32k-0613"
        ]


class VertexAI(BaseCompletionService):
    def __init__(self, vertex_project, vertex_location):
        litellm.vertex_project = vertex_project
        litellm.vertex_location = vertex_location

        self.supported_models = [
            "chat-bison-32k",
            "chat-bison",
            "chat-bison@001"
        ]


class Anthropic(BaseCompletionService):
    def __init__(self, api_key):
        litellm.anthropic_key = api_key

        self.supported_models = [
            "claude-2.1",
            "claude-2",
            "claude-instant-1",
            "claude-instant-1.2"
        ]


class Anyscale(BaseCompletionService):
    def __init__(self, api_key):
        litellm.anyscale_key = api_key

        self.supported_models = [
            "anyscale/meta-llama/Llama-2-7b-chat-hf",
            "anyscale/meta-llama/Llama-2-13b-chat-hf",
            "anyscale/meta-llama/Llama-2-70b-chat-hf",
            "anyscale/mistralai/Mistral-7B-Instruct-v0.1",
            "anyscale/codellama/CodeLlama-34b-Instruct-hf"
        ]


class Perplexity(BaseCompletionService):
    def __init__(self, api_key):
        litellm.perplexity_key = api_key

        self.supported_models = [
            "perplexity/codellama-34b-instruct",
            "perplexity/llama-2-13b-chat",
            "perplexity/llama-2-70b-chat",
            "perplexity/mistral-7b-instruct",
            "perplexity/openhermes-2-mistral-7b",
            "perplexity/openhermes-2.5-mistral-7b",
            "perplexity/pplx-7b-chat-alpha",
            "perplexity/pplx-70b-chat-alpha"
        ]


class Replicate(BaseCompletionService):
    def __init__(self, api_key):
        litellm.replicate_key = api_key

        self.supported_models = [
            "replicate/llama-2-70b-chat",
            "replicate/a16z-infra/llama-2-13b-chat",
            "replicate/vicuna-13b",
            "replicate/daanelson/flan-t5-large",
            "replicate/custom-llm-version-id",
            "replicate/deployments/ishaan-jaff/ishaan-mistral"
        ]


class TogetherAI(BaseCompletionService):
    def __init__(self, api_key):
        litellm.togetherai_key = api_key

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
            "together_ai/WizardLM/WizardLM-70B-V1.0"
        ]


class Baseten(BaseCompletionService):
    def __init__(self, api_key):
        litellm.baseten_key = api_key

        self.supported_models = [
            "baseten/qvv0xeq",
            "baseten/q841o8w",
            "baseten/31dxrj3"
        ]
