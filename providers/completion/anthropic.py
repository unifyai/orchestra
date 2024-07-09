import logging
import time
from typing import Any, List

import anthropic
from fastapi import HTTPException
from providers.completion.base_completion_provider import (
    AsyncGeneratorWrapper,
    BaseCompletionProvider,
    SyncGeneratorWrapper,
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
        message = dict(
            content=response.content[0].text,
            role="assistant",
        )
        if response.stop_reason == "tool_use":
            tool_calls = []
            for block in response.content:
                if block.type == "tool_use":
                    function_d = dict(arguments=block.input)
                    function_d["name"] = block.name
                    tool_d = dict(
                        id=block.id,
                        function=function_d,
                        type=block.type,
                    )
                    tool_calls.append(tool_d)
            message["tool_calls"] = tool_calls
        return dict(
            id=response.id,
            choices=[
                dict(
                    finish_reason=response.stop_reason,
                    index=0,
                    message=message,
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
            # any messages with the system role is removed and passed explicitely
            messages, system_prompt = _pop_system_prompts(messages)
            if system_prompt:
                kwargs["system"] = system_prompt
            if "tools" in kwargs:
                kwargs["tools"] = _format_tools_to_anthropic(kwargs["tools"])
            if "tool_choice" in kwargs:
                if "tool_choice" == "auto":
                    kwargs["tool_choice"] = {"type": "auto"}
                elif "tool_choice" == "required":
                    kwargs["tool_choice"] = {"type": "any"}
                elif "type" in kwargs["tool_choice"] and kwargs["tool_choice"]["type"] == "function":
                    tool_name = kwargs["tool_choice"]["function"]["name"]
                    kwargs["tool_choice"] = {"type": "tool", "name": tool_name}
                elif kwargs["tool_choice"] == "none":
                    del kwargs["tool_choice"]
                    del kwargs["tools"]
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
        except anthropic.RateLimitError as e:
            raise HTTPException(status_code=429)
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
        except anthropic.RateLimitError as e:
            raise HTTPException(status_code=429)
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


def _pop_system_prompts(messages):
    # return a clean list of dicts and a string concatenating all system prompts.
    system_prompts = []
    clean_messages = []
    for msg in messages:
        if msg.get("role") == "system":
            system_prompts.append(msg.get("content"))
        else:
            clean_messages.append(msg)
    return clean_messages, ". ".join(system_prompts)


def _format_tools_to_anthropic(tool_list_oai):
    def _fmt_tool(tool_oai_fmt):
        tool_d = tool_oai_fmt["function"]
        tool_d["input_schema"] = tool_d.pop("parameters")
        return tool_d

    return [_fmt_tool(t) for t in tool_list_oai]


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
    "claude-3.5-sonnet": {
        "endpoint": "claude-3-5-sonnet-20240620",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
}
