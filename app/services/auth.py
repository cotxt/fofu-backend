from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models
from app.config import Settings, get_settings
from app.google_identity import GoogleIdentityError, verify_google_id_token
from app.schemas.auth import AuthResponse, TokenBundle, UserSummary
from app.security import (
    create_access_token,
    digest_refresh_token,
    hash_password,
    new_refresh_token,
    verify_password,
)
from app.services import push as push_service
from app.utils import normalize_locale


class AuthServiceError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _google_identity_conflict_error() -> AuthServiceError:
    return AuthServiceError(
        "google_identity_conflict",
        "The Google account could not be linked because the account changed concurrently.",
        status_code=409,
    )


def _guest_upgrade_conflict_error() -> AuthServiceError:
    return AuthServiceError(
        "guest_upgrade_conflict",
        "This guest account was already upgraded by another request.",
        status_code=409,
    )


@dataclass(frozen=True)
class IssuedCredentials:
    user: models.User
    session: models.AuthSession
    access_token: str
    expires_in: int
    refresh_token: str


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _revoke_active_sessions_for_guest_upgrade(db: Session, user_id: str) -> None:
    """Prevent any credential issued while guest-scoped from surviving an upgrade."""

    db.execute(
        update(models.AuthSession)
        .where(
            models.AuthSession.user_id == user_id,
            models.AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    push_service.deactivate_session_devices(db, user_id=user_id)


def _revoke_replaced_session(db: Session, *, user_id: str, session_id: str | None) -> None:
    if not session_id:
        return
    db.execute(
        update(models.AuthSession)
        .where(
            models.AuthSession.id == session_id,
            models.AuthSession.user_id == user_id,
            models.AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    push_service.deactivate_session_devices(db, session_id=session_id)


def _revoke_replaced_refresh_session(
    db: Session,
    *,
    user_id: str,
    raw_refresh_token: str | None,
) -> None:
    if not raw_refresh_token:
        return
    replaced_session_id = db.scalar(
        select(models.AuthSession.id).where(
            models.AuthSession.user_id == user_id,
            models.AuthSession.refresh_token_hash == digest_refresh_token(raw_refresh_token),
            models.AuthSession.revoked_at.is_(None),
        )
    )
    db.execute(
        update(models.AuthSession)
        .where(
            models.AuthSession.user_id == user_id,
            models.AuthSession.refresh_token_hash == digest_refresh_token(raw_refresh_token),
            models.AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    push_service.deactivate_session_devices(db, session_id=replaced_session_id)


def _claim_guest_upgrade(
    db: Session,
    *,
    user_id: str,
    values: dict[str, object],
) -> models.User:
    """Atomically claim a guest before mutating it into a full account."""

    result = db.execute(
        update(models.User)
        .where(
            models.User.id == user_id,
            models.User.is_guest.is_(True),
            models.User.is_active.is_(True),
        )
        .values(**values)
    )
    if result.rowcount != 1:
        db.rollback()
        raise _guest_upgrade_conflict_error()

    user = db.scalar(
        select(models.User)
        .where(models.User.id == user_id)
        .execution_options(populate_existing=True)
    )
    if user is None:  # Defensive: the successful UPDATE guarantees this row exists.
        db.rollback()
        raise _guest_upgrade_conflict_error()
    return user


def _reload_user(db: Session, user_id: str) -> models.User | None:
    return db.scalar(
        select(models.User)
        .where(models.User.id == user_id)
        .execution_options(populate_existing=True)
    )


def user_summary(user: models.User) -> UserSummary:
    return UserSummary(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        home_country_code=user.home_country_code,
        locale=user.locale,
        is_guest=user.is_guest,
        roles=list(user.roles or []),
    )


def token_bundle(
    credentials: IssuedCredentials,
    *,
    expose_refresh_token: bool,
) -> TokenBundle:
    return TokenBundle(
        access_token=credentials.access_token,
        expires_in=credentials.expires_in,
        refresh_token=credentials.refresh_token if expose_refresh_token else None,
        refresh_expires_at=credentials.session.expires_at,
        scope=credentials.session.scope,
    )


def auth_response(
    credentials: IssuedCredentials,
    *,
    expose_refresh_token: bool,
) -> AuthResponse:
    bundle = token_bundle(credentials, expose_refresh_token=expose_refresh_token)
    return AuthResponse(**bundle.model_dump(), user=user_summary(credentials.user))


def issue_session(
    db: Session,
    user: models.User,
    *,
    client_type: str,
    device_id: str | None,
    scope: str,
    qr_restaurant_id: str | None = None,
    access_lifetime_minutes: int | None = None,
    session_lifetime: timedelta | None = None,
    settings: Settings | None = None,
) -> IssuedCredentials:
    settings = settings or get_settings()
    raw_refresh_token = new_refresh_token()
    now = datetime.now(timezone.utc)
    session = models.AuthSession(
        user_id=user.id,
        refresh_token_hash=digest_refresh_token(raw_refresh_token),
        client_type=client_type,
        device_id=device_id,
        scope=scope,
        is_guest_at_issue=user.is_guest,
        qr_restaurant_id=qr_restaurant_id,
        expires_at=now + (session_lifetime or timedelta(days=settings.refresh_token_days)),
        last_used_at=now,
    )
    db.add(session)
    db.flush()
    access_token, expires_in = create_access_token(
        user_id=user.id,
        session_id=session.id,
        roles=list(user.roles or []),
        is_guest=user.is_guest,
        scope=scope,
        qr_restaurant_id=qr_restaurant_id,
        lifetime_minutes=access_lifetime_minutes,
        settings=settings,
    )
    return IssuedCredentials(
        user=user,
        session=session,
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=raw_refresh_token,
    )


def create_anonymous_user(
    db: Session,
    *,
    locale: str,
    display_name: str,
    client_type: str,
    device_id: str | None,
    settings: Settings | None = None,
) -> IssuedCredentials:
    user = models.User(
        display_name=display_name.strip() or "Guest",
        locale=normalize_locale(locale),
        is_guest=True,
        is_active=True,
        roles=[],
    )
    db.add(user)
    db.flush()
    db.add(models.FoodPassport(user_id=user.id))
    credentials = issue_session(
        db,
        user,
        client_type=client_type,
        device_id=device_id,
        scope="guest",
        settings=settings,
    )
    db.commit()
    return credentials


def register_user(
    db: Session,
    *,
    email: str,
    password: str,
    display_name: str,
    locale: str,
    home_country_code: str | None,
    client_type: str,
    device_id: str | None,
    guest_user: models.User | None = None,
    current_session_id: str | None = None,
    settings: Settings | None = None,
) -> IssuedCredentials:
    normalized_email = email.strip().casefold()
    if db.scalar(select(models.User.id).where(models.User.email == normalized_email)):
        raise AuthServiceError(
            "email_already_registered",
            "An account already exists for this email address.",
            status_code=409,
        )

    try:
        if guest_user is not None:
            if not guest_user.is_guest:
                raise AuthServiceError(
                    "already_registered",
                    "The authenticated user is already registered.",
                    status_code=409,
                )
            roles = list(guest_user.roles or [])
            if "customer" not in roles:
                roles.append("customer")
            user = _claim_guest_upgrade(
                db,
                user_id=guest_user.id,
                values={
                    "email": normalized_email,
                    "password_hash": hash_password(password),
                    "display_name": display_name.strip(),
                    "locale": normalize_locale(locale),
                    "home_country_code": home_country_code,
                    "is_guest": False,
                    "roles": roles,
                },
            )
            _revoke_active_sessions_for_guest_upgrade(db, user.id)
        else:
            user = models.User(
                email=normalized_email,
                password_hash=hash_password(password),
                display_name=display_name.strip(),
                locale=normalize_locale(locale),
                home_country_code=home_country_code,
                is_guest=False,
                is_active=True,
                roles=["customer"],
            )
            db.add(user)
            db.flush()
            db.add(models.FoodPassport(user_id=user.id))

        credentials = issue_session(
            db,
            user,
            client_type=client_type,
            device_id=device_id,
            scope="full",
            settings=settings,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AuthServiceError(
            "email_already_registered",
            "An account already exists for this email address.",
            status_code=409,
        ) from exc
    return credentials


def login_user(
    db: Session,
    *,
    email: str,
    password: str,
    client_type: str,
    device_id: str | None,
    scope: str = "full",
    settings: Settings | None = None,
) -> IssuedCredentials:
    user = db.scalar(select(models.User).where(models.User.email == email.strip().casefold()))
    valid = False
    if user is not None and user.password_hash:
        try:
            valid = verify_password(password, user.password_hash)
        except Exception:  # A corrupt legacy hash must not reveal account state.
            valid = False
    if not valid or user is None or user.is_guest or not user.is_active:
        raise AuthServiceError("invalid_credentials", "Invalid email or password.")

    credentials = issue_session(
        db,
        user,
        client_type=client_type,
        device_id=device_id,
        scope=scope,
        settings=settings,
    )
    db.commit()
    return credentials


def login_google_user(
    db: Session,
    *,
    id_token: str,
    locale: str,
    client_type: str,
    device_id: str | None,
    current_user: models.User | None = None,
    current_session_id: str | None = None,
    replaced_refresh_token: str | None = None,
    settings: Settings | None = None,
) -> IssuedCredentials:
    settings = settings or get_settings()
    current_user_was_guest = current_user is not None and current_user.is_guest
    try:
        google_identity = verify_google_id_token(id_token, settings=settings)
    except GoogleIdentityError as exc:
        raise AuthServiceError(exc.code, exc.message, status_code=exc.status_code) from exc

    if current_user is not None:
        refreshed_current_user = _reload_user(db, current_user.id)
        if refreshed_current_user is None or not refreshed_current_user.is_active:
            raise AuthServiceError("account_inactive", "This account is not active.")
        current_user = refreshed_current_user

    identity = db.scalar(
        select(models.AuthIdentity)
        .options(selectinload(models.AuthIdentity.user))
        .where(
            models.AuthIdentity.provider == "google",
            models.AuthIdentity.subject == google_identity.subject,
        )
    )
    if identity is not None:
        user = _reload_user(db, identity.user_id)
        if user is None:
            raise AuthServiceError("account_inactive", "This account is not active.")
        if current_user is not None:
            refreshed_current_user = _reload_user(db, current_user.id)
            if refreshed_current_user is None:
                raise AuthServiceError("account_inactive", "This account is not active.")
            current_user = refreshed_current_user
        if current_user is not None and not current_user.is_guest and current_user.id != user.id:
            raise AuthServiceError(
                "google_identity_already_linked",
                "This Google account is already linked to another Fofu account.",
                status_code=409,
            )
        if not user.is_active:
            raise AuthServiceError("account_inactive", "This account is not active.")

        try:
            identity.email = google_identity.email
            email_owner_id = db.scalar(
                select(models.User.id).where(
                    models.User.email == google_identity.email,
                    models.User.id != user.id,
                )
            )
            if user.password_hash is None and email_owner_id is None:
                user.email = google_identity.email
            if current_user is not None:
                _revoke_replaced_session(
                    db,
                    user_id=current_user.id,
                    session_id=current_session_id,
                )
                _revoke_replaced_refresh_session(
                    db,
                    user_id=current_user.id,
                    raw_refresh_token=replaced_refresh_token,
                )
            credentials = issue_session(
                db,
                user,
                client_type=client_type,
                device_id=device_id,
                scope="full",
                settings=settings,
            )
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise _google_identity_conflict_error() from exc
        return credentials

    if current_user is not None and not current_user_was_guest:
        existing_provider_identity = db.scalar(
            select(models.AuthIdentity.id).where(
                models.AuthIdentity.user_id == current_user.id,
                models.AuthIdentity.provider == "google",
            )
        )
        if existing_provider_identity is not None:
            raise AuthServiceError(
                "google_identity_already_linked",
                "This Fofu account is already linked to a different Google account.",
                status_code=409,
            )

    email_owner = db.scalar(
        select(models.User).where(models.User.email == google_identity.email)
    )
    try:
        if current_user is None:
            if email_owner is not None:
                raise AuthServiceError(
                    "google_account_link_required",
                    "Sign in to the existing account before linking Google.",
                    status_code=409,
                )
            user = models.User(
                email=google_identity.email,
                display_name=google_identity.display_name,
                locale=normalize_locale(locale),
                is_guest=False,
                is_active=True,
                roles=["customer"],
            )
            db.add(user)
            db.flush()
            db.add(models.FoodPassport(user_id=user.id))
        elif current_user_was_guest:
            if email_owner is not None and email_owner.id != current_user.id:
                raise AuthServiceError(
                    "google_account_link_required",
                    "Sign in to the existing account before linking Google.",
                    status_code=409,
                )
            roles = list(current_user.roles or [])
            if "customer" not in roles:
                roles.append("customer")
            user = _claim_guest_upgrade(
                db,
                user_id=current_user.id,
                values={
                    "email": google_identity.email,
                    "display_name": google_identity.display_name,
                    "locale": normalize_locale(locale),
                    "is_guest": False,
                    "roles": roles,
                },
            )
            _revoke_active_sessions_for_guest_upgrade(db, user.id)
        else:
            if email_owner is not None and email_owner.id != current_user.id:
                raise AuthServiceError(
                    "google_account_link_required",
                    "This Google email belongs to another Fofu account.",
                    status_code=409,
                )
            if not current_user.is_active:
                raise AuthServiceError("account_inactive", "This account is not active.")
            user = current_user
            _revoke_replaced_session(
                db,
                user_id=user.id,
                session_id=current_session_id,
            )
            _revoke_replaced_refresh_session(
                db,
                user_id=user.id,
                raw_refresh_token=replaced_refresh_token,
            )

        db.add(
            models.AuthIdentity(
                user_id=user.id,
                provider="google",
                subject=google_identity.subject,
                email=google_identity.email,
            )
        )
        credentials = issue_session(
            db,
            user,
            client_type=client_type,
            device_id=device_id,
            scope="full",
            settings=settings,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _google_identity_conflict_error() from exc
    return credentials


def rotate_refresh_token(
    db: Session,
    raw_refresh_token: str,
    *,
    settings: Settings | None = None,
) -> IssuedCredentials:
    settings = settings or get_settings()
    session = db.scalar(
        select(models.AuthSession)
        .options(selectinload(models.AuthSession.user))
        .where(models.AuthSession.refresh_token_hash == digest_refresh_token(raw_refresh_token))
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    now = datetime.now(timezone.utc)
    if (
        session is None
        or session.revoked_at is not None
        or _aware(session.expires_at) <= now
        or not session.user.is_active
        or session.is_guest_at_issue != session.user.is_guest
    ):
        raise AuthServiceError("invalid_refresh_token", "Invalid or expired refresh token.")

    replacement = new_refresh_token()
    session.refresh_token_hash = digest_refresh_token(replacement)
    session.last_used_at = now
    if session.scope == "qr_guest":
        remaining = _aware(session.expires_at) - now
        access_minutes = max(
            1, min(settings.qr_guest_token_minutes, int(remaining.total_seconds() / 60))
        )
    else:
        session.expires_at = now + timedelta(days=settings.refresh_token_days)
        access_minutes = settings.access_token_minutes

    access_token, expires_in = create_access_token(
        user_id=session.user.id,
        session_id=session.id,
        roles=list(session.user.roles or []),
        is_guest=session.user.is_guest,
        scope=session.scope,
        qr_restaurant_id=session.qr_restaurant_id,
        lifetime_minutes=access_minutes,
        settings=settings,
    )
    db.commit()
    return IssuedCredentials(
        user=session.user,
        session=session,
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=replacement,
    )


def revoke_sessions(
    db: Session,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    raw_refresh_token: str | None = None,
    all_sessions: bool = False,
) -> bool:
    now = datetime.now(timezone.utc)
    target = None
    if raw_refresh_token:
        target = db.scalar(
            select(models.AuthSession).where(
                models.AuthSession.refresh_token_hash == digest_refresh_token(raw_refresh_token)
            )
        )
    elif session_id:
        target = db.get(models.AuthSession, session_id)

    if all_sessions:
        if not user_id:
            raise AuthServiceError("authentication_required", "Authentication is required.")
        result = db.execute(
            update(models.AuthSession)
            .where(
                models.AuthSession.user_id == user_id,
                models.AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        deactivated_devices = push_service.deactivate_session_devices(db, user_id=user_id)
        db.commit()
        return bool(result.rowcount or deactivated_devices)

    if target is None or (user_id is not None and target.user_id != user_id):
        return False
    session_changed = target.revoked_at is None
    if session_changed:
        target.revoked_at = now
    deactivated_devices = push_service.deactivate_session_devices(
        db,
        session_id=target.id,
    )
    if session_changed or deactivated_devices:
        db.commit()
    return True
