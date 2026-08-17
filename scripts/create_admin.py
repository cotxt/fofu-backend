from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timezone

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app import models
from app.database import SessionLocal
from app.security import hash_password

PASSWORD_ENV_NAME = "FOFU_ADMIN_PASSWORD"
MIN_ADMIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
EMAIL_ADAPTER = TypeAdapter(EmailStr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a registered Fofu administrator or promote an existing account. "
            "The password is read interactively unless FOFU_ADMIN_PASSWORD is set."
        )
    )
    parser.add_argument("--email", required=True, help="Administrator email address")
    parser.add_argument(
        "--display-name",
        help="Display name for a new account, or a replacement name for an existing account",
    )
    return parser.parse_args()


def normalize_email(raw_email: str) -> str:
    try:
        email = EMAIL_ADAPTER.validate_python(raw_email.strip())
    except ValidationError as exc:
        raise ValueError("Enter a valid email address.") from exc
    return str(email).casefold()


def read_password() -> str:
    password = os.environ.get(PASSWORD_ENV_NAME)
    if password is None:
        password = getpass.getpass("Admin password: ")
        confirmation = getpass.getpass("Confirm admin password: ")
        if password != confirmation:
            raise ValueError("The password confirmation does not match.")

    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise ValueError(
            f"The admin password must contain at least {MIN_ADMIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"The admin password must contain at most {MAX_PASSWORD_LENGTH} characters."
        )
    if not password.strip():
        raise ValueError("The admin password cannot be blank.")
    return password


def validate_display_name(raw_display_name: str | None) -> str | None:
    if raw_display_name is None:
        return None
    display_name = raw_display_name.strip()
    if not display_name:
        raise ValueError("The display name cannot be blank.")
    if len(display_name) > 100:
        raise ValueError("The display name must contain at most 100 characters.")
    return display_name


def provision_admin(
    *,
    email: str,
    password: str,
    display_name: str | None,
) -> tuple[bool, int]:
    now = datetime.now(timezone.utc)
    encoded_password = hash_password(password)

    with SessionLocal.begin() as db:
        user = db.scalar(
            select(models.User).where(func.lower(models.User.email) == email)
        )
        created = user is None

        if user is None:
            user = models.User(
                email=email,
                password_hash=encoded_password,
                display_name=display_name or "Fofu Admin",
                locale="en",
                is_guest=False,
                is_active=True,
                roles=["admin"],
            )
            db.add(user)
            db.flush()
        else:
            user.email = email
            user.password_hash = encoded_password
            if display_name is not None:
                user.display_name = display_name
            user.is_guest = False
            user.is_active = True
            roles = list(dict.fromkeys(str(role) for role in (user.roles or []) if role))
            if "admin" not in roles:
                roles.append("admin")
            user.roles = roles

        if db.get(models.FoodPassport, user.id) is None:
            db.add(models.FoodPassport(user_id=user.id))

        revoked = db.execute(
            update(models.AuthSession)
            .where(
                models.AuthSession.user_id == user.id,
                models.AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        ).rowcount

        db.add(
            models.AuditEvent(
                actor_user_id=None,
                action="admin_account_created" if created else "admin_account_promoted",
                resource_type="user",
                resource_id=user.id,
                details={"source": "create_admin_cli", "sessions_revoked": revoked or 0},
            )
        )

    return created, revoked or 0


def main() -> int:
    args = parse_args()
    try:
        email = normalize_email(args.email)
        display_name = validate_display_name(args.display_name)
        password = read_password()
        created, revoked = provision_admin(
            email=email,
            password=password,
            display_name=display_name,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print(
            "Error: administrator provisioning failed. Check the database configuration "
            "and run migrations before retrying.",
            file=sys.stderr,
        )
        return 1

    action = "Created" if created else "Updated"
    print(f"{action} administrator account: {email}")
    print(f"Revoked active sessions: {revoked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
