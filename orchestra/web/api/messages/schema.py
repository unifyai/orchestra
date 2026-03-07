from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


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
