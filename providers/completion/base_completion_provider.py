import litellm
import openai


class BaseCompletionProvider:
    def __init__(self):
        self.supported_models = []
        self.model = None

    def set_api_key(self, api_key):
        litellm.api_key = api_key

    def complete(self, model, messages, max_tokens, temperature):
        if model not in self.supported_models:
            raise ValueError("Model not supported")

        if model is None:
            model = self.model

        try:
            return litellm.completion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except openai.APITimeoutError as error:
            print(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            print(f"Raised error type: {type(error)}, Error: {error}")
