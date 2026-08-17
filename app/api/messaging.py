from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from app.dependencies import CurrentPrincipal, CurrentUser, DBSession
from app.schemas.messaging import (
    ConversationCreate,
    ConversationListResponse,
    ConversationResponse,
    MessageCreate,
    MessageListResponse,
    MessageResponse,
    ReadReceiptResponse,
)
from app.services import messaging as messaging_service

router = APIRouter(prefix="/conversations", tags=["messaging"])


@router.get("", response_model=ConversationListResponse)
def get_conversations(
    db: DBSession,
    user: CurrentUser,
    inbox_filter: Annotated[
        Literal["all", "unread", "restaurants"], Query(alias="filter")
    ] = "all",
    q: Annotated[str | None, Query(max_length=100)] = None,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> ConversationListResponse:
    return messaging_service.list_conversations(
        db,
        user,
        inbox_filter=inbox_filter,
        query=q,
        cursor=cursor,
        limit=limit,
    )


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def post_conversation(
    payload: ConversationCreate,
    db: DBSession,
    principal: CurrentPrincipal,
) -> ConversationResponse:
    return messaging_service.create_conversation(
        db,
        principal.user,
        payload,
        session_scope=principal.claims.scope,
        scoped_restaurant_id=principal.claims.qr_restaurant_id,
    )


@router.get("/{conversation_id}/messages", response_model=MessageListResponse)
def get_messages(
    conversation_id: str,
    db: DBSession,
    user: CurrentUser,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessageListResponse:
    return messaging_service.list_messages(
        db,
        user,
        conversation_id,
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_message(
    conversation_id: str,
    payload: MessageCreate,
    db: DBSession,
    user: CurrentUser,
) -> MessageResponse:
    return messaging_service.send_message(db, user, conversation_id, payload)


@router.post("/{conversation_id}/read", response_model=ReadReceiptResponse)
def post_read_receipt(
    conversation_id: str,
    db: DBSession,
    user: CurrentUser,
) -> ReadReceiptResponse:
    return messaging_service.mark_conversation_read(db, user, conversation_id)
