from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AttachmentRef(BaseModel):
    """Reference to a previously uploaded file attachment."""

    id: str = Field(
        ...,
        description="Unique identifier returned by the upload endpoint.",
    )
    filename: str = Field(
        ...,
        description="Original filename of the attachment.",
    )
    gs_url: str = Field(
        ...,
        description="Google Cloud Storage URI (gs://bucket/path).",
    )
    signed_url: Optional[str] = Field(
        None,
        description="Pre-signed download URL. Returned by the server; omit when sending.",
    )
    content_type: Optional[str] = Field(
        None,
        description="MIME type of the file.",
    )
    size_bytes: Optional[int] = Field(
        None,
        description="File size in bytes.",
    )


class MessageSend(BaseModel):
    assistant_id: int = Field(
        ...,
        description="The ID of the assistant to send the message to.",
        example=42,
    )
    message: str = Field(
        ...,
        description="The message content.",
        min_length=1,
        example="Add milk to my shopping list.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Optional tags for routing and context (e.g. ['source:slack', 'channel:#general']).",
    )
    attachments: list[AttachmentRef] = Field(
        default_factory=list,
        description="File attachments previously uploaded via POST /messages/attachments.",
    )


class MessageStatus(BaseModel):
    message_id: str = Field(
        ...,
        description="Unique identifier for this message. Use this to poll for the assistant's response.",
    )
    assistant_id: int = Field(
        ...,
        description="The ID of the assistant this message was sent to.",
    )
    message: str = Field(
        ...,
        description="The original message that was sent.",
    )
    status: str = Field(
        ...,
        description="Current status: 'processing' or 'completed'.",
    )
    response: Optional[str] = Field(
        None,
        description="The assistant's response, if any. Null while processing or if the assistant chose not to respond on this channel.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags supplied with the original message.",
    )
    attachments: list[AttachmentRef] = Field(
        default_factory=list,
        description="File attachments supplied with the original message.",
    )
    response_tags: Optional[list[str]] = Field(
        None,
        description="Tags attached to the assistant's response. Null while processing.",
    )
    response_attachments: Optional[list[AttachmentRef]] = Field(
        None,
        description="File attachments in the assistant's response. Null while processing.",
    )
    created_at: datetime = Field(
        ...,
        description="When the message was sent.",
    )
    completed_at: Optional[datetime] = Field(
        None,
        description="When the assistant finished processing. Null while still processing.",
    )


class MessageComplete(BaseModel):
    response: Optional[str] = Field(
        None,
        description="The assistant's response text. Null if the assistant chose not to respond.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for the response (typically echoed from the inbound message).",
    )
    attachments: list[AttachmentRef] = Field(
        default_factory=list,
        description="File attachments in the response.",
    )
