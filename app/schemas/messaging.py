from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ParticipantResponse(BaseModel):
    user_id: str
    display_name: str
    joined_at: datetime
    last_read_at: datetime | None


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender_user_id: str
    sender_display_name: str
    body: str
    kind: str
    media_asset_id: str | None
    created_at: datetime
    edited_at: datetime | None
    client_message_id: str | None = None
    idempotency_replayed: bool = False


class ConversationResponse(BaseModel):
    id: str
    kind: str
    restaurant_id: str | None
    title: str | None
    participants: list[ParticipantResponse]
    last_message: MessageResponse | None
    last_message_at: datetime
    unread_count: int
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]
    next_cursor: str | None = None
    has_more: bool = False


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["direct", "restaurant"] = "direct"
    participant_user_ids: list[str] = Field(default_factory=list, max_length=20)
    restaurant_id: str | None = Field(default=None, min_length=1, max_length=36)
    title: str | None = Field(default=None, max_length=160)

    @field_validator("participant_user_ids")
    @classmethod
    def normalize_participants(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = raw.strip()
            if not value or len(value) > 36:
                raise ValueError("participant_user_ids contains an invalid user id")
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        return value or None

    @model_validator(mode="after")
    def validate_kind(self) -> ConversationCreate:
        if self.kind == "direct":
            if self.restaurant_id is not None:
                raise ValueError("restaurant_id is only valid for restaurant conversations")
            if len(self.participant_user_ids) != 1:
                raise ValueError("A direct conversation requires exactly one other participant")
        elif self.restaurant_id is None:
            raise ValueError("restaurant_id is required for a restaurant conversation")
        return self


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(default="", max_length=4000)
    kind: Literal["text", "image", "document"] = "text"
    media_asset_id: str | None = Field(default=None, min_length=1, max_length=36)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        return value.strip()

    @field_validator("client_message_id")
    @classmethod
    def normalize_client_message_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("client_message_id contains invalid characters")
        return value

    @model_validator(mode="after")
    def validate_content(self) -> MessageCreate:
        if self.kind == "text":
            if not self.body:
                raise ValueError("Text messages cannot be empty")
            if self.media_asset_id is not None:
                raise ValueError("Text messages cannot include a media asset")
        elif self.media_asset_id is None:
            raise ValueError("Media messages require media_asset_id")
        return self


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    next_cursor: str | None = None
    has_more: bool = False


class ReadReceiptResponse(BaseModel):
    conversation_id: str
    user_id: str
    last_read_at: datetime
