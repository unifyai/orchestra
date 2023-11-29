from providers.completion.anyscale import Anyscale
from providers.completion.replicate import Replicate
from providers.completion.perplexity import Perplexity
from providers.completion.together_ai import TogetherAI


llama2_7b_chat = {
    "anyscale": "anyscale/meta-llama/Llama-2-7b-chat-hf",
    "perplexity": "perplexity/pplx-7b-chat-alpha",
    "together_ai": "together_ai/togethercomputer/llama-2-7b"
}

llama2_13b_chat = {
    "anyscale": "anyscale/meta-llama/Llama-2-13b-chat-hf",
    "perplexity": "perplexity/llama-2-13b-chat",
    "replicate": "replicate/a16z-infra/llama-2-13b-chat"
}

llama2_70b_chat = {
    "anyscale": "anyscale/meta-llama/Llama-2-70b-chat-hf",
    "perplexity": "perplexity/llama-2-70b-chat",
    "replicate": "replicate/llama-2-70b-chat",
    "together_ai": "together_ai/togethercomputer/llama-2-70b-chat",
}


class Llama7BChat:
    def __init__(self, provider):
        if provider not in llama2_7b_chat:
            raise Exception("Invalid model")

        if provider == "anyscale":
            self.provider_obj = Anyscale()
        elif provider == "perplexity":
            self.provider_obj = Perplexity()
        elif provider == "replicate":
            self.provider_obj = Replicate()
        elif provider == "together_ai":
            self.provider_obj = TogetherAI()
        else:
            raise Exception("Invalid provider")

        self.provider_obj.model = llama2_7b_chat[provider]

    def set_api_key(self, api_key):
        self.provider_obj.set_api_key(api_key)

    def get_completion(self, prompt, max_tokens=16, temperature=0.9):
        return self.provider_obj.complete(prompt, max_tokens, temperature)


class Llama70BChat:
    def __init__(self, provider):
        if provider not in llama2_70b_chat:
            raise Exception("Invalid model")

        if provider == "anyscale":
            self.provider_obj = Anyscale()
        elif provider == "perplexity":
            self.provider_obj = Perplexity()
        elif provider == "replicate":
            self.provider_obj = Replicate()
        elif provider == "together_ai":
            self.provider_obj = TogetherAI()
        else:
            raise Exception("Invalid provider")

        self.provider_obj.model = llama2_70b_chat[provider]

    def set_api_key(self, api_key):
        self.provider_obj.set_api_key(api_key)

    def get_completion(self, prompt, max_tokens=16, temperature=0.9):
        messages = [{"content": prompt, "role": "user"}]
        return self.provider_obj.complete(self.provider_obj.model, messages, max_tokens, temperature)
