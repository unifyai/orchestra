import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class VertexAI(BaseCompletionProvider):
    def __init__(self):
        # https://cloud.google.com/vertex-ai/docs/generative-ai/learn/models
        self.supported_models = [
            "text-bison",
            "chat-bison",
            "code-bison",
            "codechat-bison",
            "code-gecko",
            "text-bison-32k",
            "chat-bison-32k",
            "code-bison-32k",
            "codechat-bison-32k",
        ]

    def set_project(self, vertex_project):
        litellm.vertex_project = vertex_project

    def set_location(self, vertex_location):
        litellm.vertex_location = vertex_location
