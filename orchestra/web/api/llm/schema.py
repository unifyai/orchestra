from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat-completions request.

    Only ``model`` and ``messages`` are validated; every other OpenAI /
    OpenRouter field (``temperature``, ``response_format``, ``tools``,
    ``stream_options``, provider routing hints, …) is accepted verbatim via
    ``extra="allow"`` and forwarded to the provider unchanged.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    # Optional attribution the caller may pass so per-assistant spend is
    # itemised in the credit ledger. Falls back to the API key's identity.
    assistant_id: int | None = Field(default=None)
