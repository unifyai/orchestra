import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class VertexAI(BaseCompletionProvider):
    def __init__(self):
        self.supported_models = [
            "chat-bison-32k",
            "chat-bison",
            "chat-bison@001",
        ]

    def set_project(self, vertex_project):
        litellm.vertex_project = vertex_project

    def set_location(self, vertex_location):
        litellm.vertex_location = vertex_location
