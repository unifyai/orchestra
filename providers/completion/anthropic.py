import logging

from providers.completion.base_completion_provider import BaseCompletionProvider

logger = logging.getLogger(__name__)


class Anthropic(BaseCompletionProvider):
    """
    A completion provider that uses the Anthropic service.

    Source: https://docs.anthropic.com/claude/docs/models-overview
    Pricing is per million tokens: https://docs.anthropic.com/claude/docs/models-overview#model-comparison
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "anthropic",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_ANTHROPIC_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "ANTHROPIC_API_KEY"


supported_models = {
    "claude-3-haiku": {
        "endpoint": "anthropic/claude-3-haiku-20240307",
        "context_window": 200000,
        "cost": {"prompt": 0.25, "completion": 1.25},
    },
    "claude-3-sonnet": {
        "endpoint": "anthropic/claude-3-sonnet-20240229",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3-opus": {
        "endpoint": "anthropic/claude-3-opus-20240229",
        "context_window": 200000,
        "cost": {"prompt": 15, "completion": 75},
    },
    "claude-3.5-sonnet": {
        "endpoint": "anthropic/claude-3-5-sonnet-20241022",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3.5-sonnet-20240620": {
        "endpoint": "anthropic/claude-3-5-sonnet-20240620",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
}
