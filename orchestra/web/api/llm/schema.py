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


class AuthorizeRequest(BaseModel):
    """Ask whether a call may proceed, without sending it through us.

    The metering half of the gateway. A caller that holds the provider key
    itself — the pod-local broker sidecar — streams bytes straight to the
    provider and uses this to ask the same questions the proxy routes answer
    inline: is the account in good standing, is it within its caps, and can
    this model be priced at all. Milliseconds of metadata rather than a
    connection held open for the length of a generation.
    """

    model: str
    assistant_id: int | None = Field(default=None)


class AuthorizeResponse(BaseModel):
    """The verdict, plus what the caller needs to report usage back."""

    allowed: bool
    reason: str | None = None
    #: Echoed on the matching settle call so the two are unambiguously the
    #: same call, and so a settle for a call that was never authorised is
    #: recognisable rather than silently accepted.
    lease: str | None = None


class SettleRequest(BaseModel):
    """Report a completed call's usage so it can be charged.

    ``usage`` is the provider's own usage object, forwarded verbatim: the
    OpenRouter shape carries an authoritative ``cost``, the Anthropic shape
    carries token counts that are priced from our catalogue. Sending it
    unaltered keeps the pricing decision on this side, where refusing an
    unpriceable model already lives.
    """

    model: str
    usage: dict[str, Any]
    assistant_id: int | None = Field(default=None)
    lease: str | None = None


class SettleResponse(BaseModel):
    charged: float
    #: False when the deployment does not charge (self-host, staging) or the
    #: usage carried nothing priceable — distinguishable from a zero charge.
    metered: bool
