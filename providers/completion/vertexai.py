import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class VertexAI(BaseCompletionProvider):
    """
    A completion provider that uses the VertexAI service.

    Supported models: https://cloud.google.com/vertex-ai/docs/generative-ai/learn/models
    """

    supported_models = {
        "text-bison": {
            "endpoint": "text-bison",
            "context_window": 8192,
            "cost": {"prompt": -1, "completion": -1},
        },
        "chat-bison": {
            "endpoint": "chat-bison",
            "context_window": 8192,
            "cost": {"prompt": -1, "completion": -1},
        },
        "code-bison": {
            "endpoint": "code-bison",
            "context_window": 6144,
            "cost": {"prompt": -1, "completion": -1},
        },
        "codechat-bison": {
            "endpoint": "codechat-bison",
            "context_window": 6144,
            "cost": {"prompt": -1, "completion": -1},
        },
        "code-gecko": {
            "endpoint": "code-gecko",
            "context_window": 2048,
            "cost": {"prompt": -1, "completion": -1},
        },
        "text-bison-32k": {
            "endpoint": "text-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": -1, "completion": -1},
        },
        "chat-bison-32k": {
            "endpoint": "chat-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": -1, "completion": -1},
        },
        "code-bison-32k": {
            "endpoint": "code-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": -1, "completion": -1},
        },
        "codechat-bison-32k": {
            "endpoint": "codechat-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": -1, "completion": -1},
        },
    }

    def set_project(self, vertex_project: str) -> None:  # noqa: D102
        litellm.vertex_project = vertex_project

    def set_location(self, vertex_location: str) -> None:  # noqa: D102
        litellm.vertex_location = vertex_location
