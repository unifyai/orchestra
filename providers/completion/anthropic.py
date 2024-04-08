import logging
import time
from typing import Any, List

import anthropic
from providers.completion.base_completion_provider import (
    BaseCompletionProvider,
    SyncGeneratorWrapper,
    AsyncGeneratorWrapper,
)

from orchestra.web.api.utils.http_responses import server_error_with_digest

logger = logging.getLogger(__name__)


class Anthropic(BaseCompletionProvider):
    """
    A completion provider that uses the Anthropic service.

    Source: https://docs.anthropic.com/claude/docs/models-overview
    Pricing is per million tokens: https://docs.anthropic.com/claude/docs/models-overview#model-comparison
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.async_client = anthropic.AsyncAnthropic(api_key=self.api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_ANTHROPIC_API_KEY"

    @staticmethod
    def usage_from_response(response):
        return {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
        }

    def response_to_chat_completion(self, response):
        created_at = int(time.time())
        return dict(
            id=response.id,
            choices=[
                dict(
                    finish_reason=response.content[0].text,
                    index=0,
                    message=dict(
                        content=response.content[0].text,
                        role="assistant",
                    ),
                    logprobs=None,
                ),
            ],
            created=created_at,
            model=self.hub_model,
            object="chat.completion",
            usage=self.usage_from_response(response),
        )

    def __call__(self, messages: List, stream: bool = False, **kwargs: Any) -> Any:
        try:
            max_tokens = kwargs.pop("max_tokens", 1024)
            response = self.client.messages.create(
                messages=messages,
                model=self.provider_endpoint,
                stream=stream,
                max_tokens=max_tokens,
                **kwargs,
            )
            if stream:
                return (AnthropicSyncGeneratorWrapper(self, response, messages), None)
            else:
                return (
                    self.response_to_chat_completion(response),
                    self.compute_cost(
                        response.usage.input_tokens,
                        response.usage.output_tokens,
                    ),
                )
        except Exception as e:
            error, digest = server_error_with_digest(str(e))
            logger.error(f"Digest {digest}: {e}")
            raise error

    def __call_async__(
        self, messages: List, stream: bool = False, **kwargs: Any
    ) -> Any:
        try:
            max_tokens = kwargs.pop("max_tokens", 1024)
            response = self.async_client.messages.create(
                messages=messages,
                model=self.provider_endpoint,
                stream=stream,
                max_tokens=max_tokens,
                **kwargs,
            )
            if stream:
                return (AnthropicAsyncGeneratorWrapper(self, response, messages), None)
            else:  # TODO: This is not working (in most (all?) providers tbh)
                return (
                    self.response_to_chat_completion(response),
                    self.compute_cost(
                        response.usage.input_tokens,
                        response.usage.output_tokens,
                    ),
                )
        except Exception as e:
            error, digest = server_error_with_digest(str(e))
            logger.error(f"Digest {digest}: {e}")
            raise error


class AnthropicSyncGeneratorWrapper(SyncGeneratorWrapper):
    def generator_iteration(self, part, whole):
        return sse_to_part_dict(part, whole)


class AnthropicAsyncGeneratorWrapper(AsyncGeneratorWrapper):
    def generator_iteration(self, part, whole):
        return sse_to_part_dict(part, whole)


_sse_types_to_ignore = {
    "message_start",
    "content_block_start",
    "content_block_stop",
    "message_delta",
}


def sse_to_part_dict(part, whole):
    if part.type in _sse_types_to_ignore:
        return None

    finish_reason = None
    if part.type == "message_stop":
        content = ""
        finish_reason = "stop"
    else:
        content = part.delta.text if hasattr(part.delta, "text") else ""

    part_dict = {
        "id": "msg_id",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "logprobs": None,  # TODO
                "finish_reason": finish_reason,
            },
        ],
        "usage": {},
    }

    if part.type == "message_stop":
        return part_dict
    if not hasattr(part.delta, "text") or not part.delta.text:
        return None
    if not whole:
        whole.extend([""])
    whole[0] += part.delta.text
    return part_dict


supported_models = {
    "claude-3-haiku": {
        "endpoint": "claude-3-haiku-20240307",
        "context_window": 200000,
        "cost": {"prompt": 0.25, "completion": 1.25},
    },
    "claude-3-sonnet": {
        "endpoint": "claude-3-sonnet-20240229",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3-opus": {
        "endpoint": "claude-3-opus-20240229",
        "context_window": 200000,
        "cost": {"prompt": 15, "completion": 75},
    },
}
