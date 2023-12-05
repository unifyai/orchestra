import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class VertexAI(BaseCompletionProvider):
    """
    A completion provider that uses the VertexAI service.

    Supported models: https://cloud.google.com/vertex-ai/docs/generative-ai/learn/models
    """

    supported_models = {
        "text-bison",
        "chat-bison",
        "code-bison",
        "codechat-bison",
        "code-gecko",
        "text-bison-32k",
        "chat-bison-32k",
        "code-bison-32k",
        "codechat-bison-32k",
    }

    def set_project(self, vertex_project: str) -> None:  # noqa: D102
        litellm.vertex_project = vertex_project

    def set_location(self, vertex_location: str) -> None:  # noqa: D102
        litellm.vertex_location = vertex_location
