import litellm
import openai


class BaseCompletionProvider:
    def __init__(self):
        self.supported_models = []
        self.model = None

    def set_api_key(self, api_key):
        pass

    def complete(self, model, messages, max_tokens, temperature):
        if model not in self.supported_models:
            raise Exception("Model not supported")

        if model == None:
            model = self.model

        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
            return response
        except openai.APITimeoutError as e:
            print(f"Raised error type: {type(e)}, Error: {e}")
            pass
        except Exception as e:
            print(f"Raised error type: {type(e)}, Error: {e}")
            pass
