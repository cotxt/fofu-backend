from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app import models
from app.schemas.messaging import (
    ConversationCreate,
    ConversationListResponse,
    ConversationResponse,
    MessageCreate,
    MessageListResponse,
    MessageResponse,
    ParticipantResponse,
    ReadReceiptResponse,
)
from app.services import push as push_service
from app.utils import decode_cursor, encode_cursor


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _audit(
    db: Session,
    *,
    actor_user_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, object] | None = None,
) -> None:
    db.add(
        models.AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
    )


def _participant_or_404(
    db: Session, conversation_id: str, user_id: str
) -> tuple[models.Conversation, models.ConversationParticipant]:
    row = db.execute(
        select(models.Conversation, models.ConversationParticipant)
        .join(
            models.ConversationParticipant,
            models.ConversationParticipant.conversation_id == models.Conversation.id,
        )
        .where(
            models.Conversation.id == conversation_id,
            models.ConversationParticipant.user_id == user_id,
        )
    ).one_or_none()
    if row is None:
        # A non-participant receives the same response as an unknown conversation.
        raise _error(status.HTTP_404_NOT_FOUND, "conversation_not_found", "Conversation not found.")
    return row


def _participant_ids(db: Session, conversation_id: str) -> list[str]:
    return list(
        db.scalars(
            select(models.ConversationParticipant.user_id)
            .where(models.ConversationParticipant.conversation_id == conversation_id)
            .order_by(models.ConversationParticipant.user_id)
        ).all()
    )


def _find_existing_conversation(
    db: Session,
    *,
    user_id: str,
    kind: str,
    restaurant_id: str | None,
    participant_ids: set[str],
) -> models.Conversation | None:
    candidates = db.scalars(
        select(models.Conversation)
        .join(
            models.ConversationParticipant,
            models.ConversationParticipant.conversation_id == models.Conversation.id,
        )
        .where(
            models.ConversationParticipant.user_id == user_id,
            models.Conversation.kind == kind,
            models.Conversation.restaurant_id == restaurant_id,
        )
        .order_by(models.Conversation.created_at.desc())
    ).all()
    for conversation in candidates:
        if set(_participant_ids(db, conversation.id)) == participant_ids:
            return conversation
    return None


def _restaurant_staff_ids(db: Session, restaurant: models.Restaurant) -> set[str]:
    return set(
        db.scalars(
            select(models.RestaurantMembership.user_id).where(
                models.RestaurantMembership.restaurant_id == restaurant.id,
                models.RestaurantMembership.status == "active",
                or_(
                    models.RestaurantMembership.role != "owner",
                    models.RestaurantMembership.user_id == restaurant.owner_user_id,
                ),
            )
        ).all()
    )


def _message_response(
    db: Session,
    message: models.Message,
    *,
    client_message_id: str | None = None,
    replayed: bool = False,
) -> MessageResponse:
    sender_name = db.scalar(
        select(models.User.display_name).where(models.User.id == message.sender_user_id)
    )
    return MessageResponse(
        id=message.id,
        conversation_id=message.conversation_id,
        sender_user_id=message.sender_user_id,
        sender_display_name=sender_name or "Unknown user",
        body=message.body,
        kind=message.kind,
        media_asset_id=message.media_asset_id,
        created_at=message.created_at,
        edited_at=message.edited_at,
        client_message_id=(
            client_message_id if client_message_id is not None else message.client_message_id
        ),
        idempotency_replayed=replayed,
    )


def _conversation_response(
    db: Session, conversation: models.Conversation, user_id: str
) -> ConversationResponse:
    participant_rows = db.execute(
        select(models.ConversationParticipant, models.User)
        .join(models.User, models.User.id == models.ConversationParticipant.user_id)
        .where(models.ConversationParticipant.conversation_id == conversation.id)
        .order_by(models.ConversationParticipant.joined_at, models.User.id)
    ).all()
    current_participant = next(
        (participant for participant, _ in participant_rows if participant.user_id == user_id),
        None,
    )
    last_message = db.scalar(
        select(models.Message)
        .where(
            models.Message.conversation_id == conversation.id,
            models.Message.deleted_at.is_(None),
        )
        .order_by(models.Message.created_at.desc(), models.Message.id.desc())
        .limit(1)
    )
    unread_filters = [
        models.Message.conversation_id == conversation.id,
        models.Message.sender_user_id != user_id,
        models.Message.deleted_at.is_(None),
    ]
    if current_participant is not None and current_participant.last_read_at is not None:
        unread_filters.append(models.Message.created_at > current_participant.last_read_at)
    unread_count = int(
        db.scalar(select(func.count(models.Message.id)).where(*unread_filters)) or 0
    )
    return ConversationResponse(
        id=conversation.id,
        kind=conversation.kind,
        restaurant_id=conversation.restaurant_id,
        title=conversation.title,
        participants=[
            ParticipantResponse(
                user_id=participant.user_id,
                display_name=participant_user.display_name,
                joined_at=participant.joined_at,
                last_read_at=participant.last_read_at,
            )
            for participant, participant_user in participant_rows
        ],
        last_message=_message_response(db, last_message) if last_message else None,
        last_message_at=conversation.last_message_at,
        unread_count=unread_count,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def create_conversation(
    db: Session,
    user: models.User,
    payload: ConversationCreate,
    *,
    session_scope: str = "full",
    scoped_restaurant_id: str | None = None,
) -> ConversationResponse:
    if user.is_guest and (
        session_scope != "qr_guest"
        or payload.kind != "restaurant"
        or scoped_restaurant_id is None
        or payload.restaurant_id != scoped_restaurant_id
    ):
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "guest_messaging_scope_forbidden",
            "QR guests may only contact the restaurant in their scoped session.",
        )

    requested_ids = set(payload.participant_user_ids)
    requested_ids.discard(user.id)

    if payload.kind == "direct":
        if len(requested_ids) != 1:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid_participants",
                "A direct conversation requires one participant other than yourself.",
            )
        participant_ids = requested_ids | {user.id}
    else:
        restaurant = db.get(models.Restaurant, payload.restaurant_id)
        if restaurant is None:
            raise _error(status.HTTP_404_NOT_FOUND, "restaurant_not_found", "Restaurant not found.")
        staff_ids = _restaurant_staff_ids(db, restaurant)
        is_staff = user.id in staff_ids
        if not restaurant.is_published and not is_staff:
            raise _error(status.HTTP_404_NOT_FOUND, "restaurant_not_found", "Restaurant not found.")
        if requested_ids and not requested_ids.issubset(staff_ids):
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "invalid_restaurant_participants",
                "Restaurant conversations may only add active restaurant staff.",
            )
        selected_staff = requested_ids or staff_ids
        participant_ids = selected_staff | {user.id}
        if len(participant_ids) < 2:
            raise _error(
                status.HTTP_409_CONFLICT,
                "restaurant_has_no_contact",
                "This restaurant does not have an active messaging contact.",
            )

    active_ids = set(
        db.scalars(
            select(models.User.id).where(
                models.User.id.in_(participant_ids), models.User.is_active.is_(True)
            )
        ).all()
    )
    if active_ids != participant_ids:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_participants",
            "One or more participants do not exist or are inactive.",
        )

    existing = _find_existing_conversation(
        db,
        user_id=user.id,
        kind=payload.kind,
        restaurant_id=payload.restaurant_id,
        participant_ids=participant_ids,
    )
    if existing is not None:
        return _conversation_response(db, existing, user.id)

    now = models.utcnow()
    conversation = models.Conversation(
        kind=payload.kind,
        restaurant_id=payload.restaurant_id,
        title=payload.title,
        last_message_at=now,
    )
    db.add(conversation)
    db.flush()
    for participant_id in sorted(participant_ids):
        db.add(
            models.ConversationParticipant(
                conversation_id=conversation.id,
                user_id=participant_id,
                last_read_at=now if participant_id == user.id else None,
            )
        )
    _audit(
        db,
        actor_user_id=user.id,
        action="conversation.created",
        resource_type="conversation",
        resource_id=conversation.id,
        details={
            "kind": conversation.kind,
            "restaurant_id": conversation.restaurant_id,
            "participant_count": len(participant_ids),
        },
    )
    db.commit()
    db.refresh(conversation)
    return _conversation_response(db, conversation, user.id)


def list_conversations(
    db: Session,
    user: models.User,
    *,
    cursor: str | None,
    limit: int,
    inbox_filter: str = "all",
    query: str | None = None,
) -> ConversationListResponse:
    offset = decode_cursor(cursor)
    if inbox_filter not in {"all", "unread", "restaurants"}:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_conversation_filter",
            "Conversation filter must be all, unread, or restaurants.",
        )

    current_participant = aliased(models.ConversationParticipant)
    statement = (
        select(models.Conversation)
        .join(
            current_participant,
            current_participant.conversation_id == models.Conversation.id,
        )
        .where(current_participant.user_id == user.id)
    )
    if inbox_filter == "restaurants":
        statement = statement.where(models.Conversation.kind == "restaurant")
    elif inbox_filter == "unread":
        statement = statement.where(
            select(models.Message.id)
            .where(
                models.Message.conversation_id == models.Conversation.id,
                models.Message.sender_user_id != user.id,
                models.Message.deleted_at.is_(None),
                or_(
                    current_participant.last_read_at.is_(None),
                    models.Message.created_at > current_participant.last_read_at,
                ),
            )
            .exists()
        )

    normalized_query = " ".join((query or "").split())
    if normalized_query:
        escaped_query = (
            normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped_query}%"
        other_participant = aliased(models.ConversationParticipant)
        participant_user = aliased(models.User)
        participant_match = (
            select(other_participant.user_id)
            .join(participant_user, participant_user.id == other_participant.user_id)
            .where(
                other_participant.conversation_id == models.Conversation.id,
                other_participant.user_id != user.id,
                participant_user.display_name.ilike(pattern, escape="\\"),
            )
            .exists()
        )
        restaurant_match = (
            select(models.Restaurant.id)
            .where(
                models.Restaurant.id == models.Conversation.restaurant_id,
                or_(
                    models.Restaurant.name_en.ilike(pattern, escape="\\"),
                    models.Restaurant.name_ko.ilike(pattern, escape="\\"),
                ),
            )
            .exists()
        )
        latest_message_body = (
            select(models.Message.body)
            .where(
                models.Message.conversation_id == models.Conversation.id,
                models.Message.deleted_at.is_(None),
            )
            .order_by(models.Message.created_at.desc(), models.Message.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        statement = statement.where(
            or_(
                models.Conversation.title.ilike(pattern, escape="\\"),
                participant_match,
                restaurant_match,
                latest_message_body.ilike(pattern, escape="\\"),
            )
        )

    statement = (
        statement.order_by(
            models.Conversation.last_message_at.desc(),
            models.Conversation.id.desc(),
        )
        .offset(offset)
        .limit(limit + 1)
    )
    conversations = list(
        db.scalars(statement).all()
    )
    has_more = len(conversations) > limit
    conversations = conversations[:limit]
    return ConversationListResponse(
        items=[_conversation_response(db, conversation, user.id) for conversation in conversations],
        next_cursor=encode_cursor(offset + limit) if has_more else None,
        has_more=has_more,
    )


def list_messages(
    db: Session,
    user: models.User,
    conversation_id: str,
    *,
    cursor: str | None,
    limit: int,
) -> MessageListResponse:
    _participant_or_404(db, conversation_id, user.id)
    offset = decode_cursor(cursor)
    messages = list(
        db.scalars(
            select(models.Message)
            .where(
                models.Message.conversation_id == conversation_id,
                models.Message.deleted_at.is_(None),
            )
            .order_by(models.Message.created_at.desc(), models.Message.id.desc())
            .offset(offset)
            .limit(limit + 1)
        ).all()
    )
    has_more = len(messages) > limit
    messages = messages[:limit]
    messages.reverse()
    return MessageListResponse(
        items=[_message_response(db, message) for message in messages],
        next_cursor=encode_cursor(offset + limit) if has_more else None,
        has_more=has_more,
    )


def _validate_replayed_message(
    message: models.Message,
    *,
    user_id: str,
    conversation_id: str,
    payload: MessageCreate,
) -> None:
    if (
        message.sender_user_id != user_id
        or message.conversation_id != conversation_id
        or message.body != payload.body
        or message.kind != payload.kind
        or message.media_asset_id != payload.media_asset_id
    ):
        raise _error(
            status.HTTP_409_CONFLICT,
            "client_message_id_conflict",
            "client_message_id was already used with different message content.",
        )


def send_message(
    db: Session,
    user: models.User,
    conversation_id: str,
    payload: MessageCreate,
) -> MessageResponse:
    conversation, participant = _participant_or_404(db, conversation_id, user.id)

    if payload.media_asset_id is not None:
        media = db.get(models.MediaAsset, payload.media_asset_id)
        if (
            media is None
            or media.owner_user_id != user.id
            or media.status != "uploaded"
            or media.purpose != "message_attachment"
        ):
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid_media_asset",
                "The media asset is unavailable for this message.",
            )
        expected_types = (
            {"image/jpeg", "image/png"}
            if payload.kind == "image"
            else {"application/pdf"}
        )
        if media.content_type not in expected_types:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "media_kind_mismatch",
                "The media asset type does not match the message kind.",
            )

    existing = None
    if payload.client_message_id:
        existing = db.scalar(
            select(models.Message).where(
                models.Message.conversation_id == conversation_id,
                models.Message.sender_user_id == user.id,
                models.Message.client_message_id == payload.client_message_id,
            )
        )
    if existing is not None:
        _validate_replayed_message(
            existing,
            user_id=user.id,
            conversation_id=conversation_id,
            payload=payload,
        )
        return _message_response(
            db,
            existing,
            client_message_id=payload.client_message_id,
            replayed=True,
        )

    now = models.utcnow()
    message = models.Message(
        conversation_id=conversation_id,
        sender_user_id=user.id,
        body=payload.body,
        client_message_id=payload.client_message_id,
        kind=payload.kind,
        media_asset_id=payload.media_asset_id,
        created_at=now,
    )
    db.add(message)
    conversation.last_message_at = now
    participant.last_read_at = now
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if payload.client_message_id:
            existing = db.scalar(
                select(models.Message).where(
                    models.Message.conversation_id == conversation_id,
                    models.Message.sender_user_id == user.id,
                    models.Message.client_message_id == payload.client_message_id,
                )
            )
            if existing is not None:
                _validate_replayed_message(
                    existing,
                    user_id=user.id,
                    conversation_id=conversation_id,
                    payload=payload,
                )
                return _message_response(
                    db,
                    existing,
                    client_message_id=payload.client_message_id,
                    replayed=True,
                )
        raise

    _audit(
        db,
        actor_user_id=user.id,
        action="message.sent",
        resource_type="conversation",
        resource_id=conversation_id,
        details={
            "message_id": message.id,
            "kind": message.kind,
            "has_media": message.media_asset_id is not None,
            "client_message_id_hash": (
                hashlib.sha256(payload.client_message_id.encode()).hexdigest()
                if payload.client_message_id
                else None
            ),
        },
    )
    push_service.enqueue_message_notifications(db, message=message)
    db.commit()
    db.refresh(message)
    return _message_response(
        db,
        message,
        client_message_id=payload.client_message_id,
    )


def mark_conversation_read(
    db: Session, user: models.User, conversation_id: str
) -> ReadReceiptResponse:
    _, participant = _participant_or_404(db, conversation_id, user.id)
    now: datetime = models.utcnow()
    participant.last_read_at = now
    _audit(
        db,
        actor_user_id=user.id,
        action="conversation.read",
        resource_type="conversation",
        resource_id=conversation_id,
    )
    db.commit()
    return ReadReceiptResponse(
        conversation_id=conversation_id,
        user_id=user.id,
        last_read_at=now,
    )
