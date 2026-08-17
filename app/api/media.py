from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, BinaryIO

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.config import get_settings
from app.dependencies import DBSession, RegisteredUser
from app.rate_limit import SlidingWindowLimiter
from app.schemas.owner import MediaAssetResponse
from app.services import owner as owner_service

router = APIRouter(prefix="/media", tags=["media"])

_CHUNK_SIZE = 64 * 1024
_PURPOSE_CONTENT_TYPES: dict[str, set[str]] = {
    "business_license": {"image/jpeg", "image/png", "application/pdf"},
    "restaurant_cover": {"image/jpeg", "image/png"},
    "restaurant_gallery": {"image/jpeg", "image/png"},
    "menu_item": {"image/jpeg", "image/png"},
    "message_attachment": {"image/jpeg", "image/png", "application/pdf"},
    "profile_avatar": {"image/jpeg", "image/png"},
}
_EXTENSION_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".pdf": "application/pdf",
}
_CANONICAL_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/pdf": ".pdf",
}
_upload_limiter = SlidingWindowLimiter(requests=20, window_seconds=60)


def _upload_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _normalize_filename(filename: str | None) -> str:
    if not filename or "\x00" in filename:
        raise _upload_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_filename",
            "A valid filename is required.",
        )
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    basename = unicodedata.normalize("NFKC", basename).strip()
    if (
        not basename
        or basename in {".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in basename)
        or len(basename.encode("utf-8")) > 255
    ):
        raise _upload_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_filename",
            "The filename is invalid or too long.",
        )
    return basename


def _normalize_declared_content_type(content_type: str | None) -> str:
    if not content_type:
        raise _upload_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "missing_content_type",
            "The upload must declare a supported MIME type.",
        )
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized == "image/jpg":
        normalized = "image/jpeg"
    return normalized


def _detected_content_type(head: bytes, tail: bytes) -> str | None:
    trimmed_tail = tail.rstrip(b"\x00\t\r\n ")
    if head.startswith(b"\xff\xd8\xff") and trimmed_tail.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        and trimmed_tail.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")
    ):
        return "image/png"
    if head.startswith(b"%PDF-") and b"%%EOF" in trimmed_tail[-1024:]:
        return "application/pdf"
    return None


def _open_private_temp(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb")


@router.post(
    "/uploads",
    response_model=MediaAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_media(
    db: DBSession,
    user: RegisteredUser,
    purpose: Annotated[str, Form(min_length=1, max_length=40)],
    file: Annotated[UploadFile, File(...)],
) -> MediaAssetResponse:
    settings = get_settings()
    _upload_limiter.check(user.id)
    purpose = purpose.strip().lower()
    allowed_content_types = _PURPOSE_CONTENT_TYPES.get(purpose)
    if allowed_content_types is None:
        raise _upload_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_upload_purpose",
            "The upload purpose is not supported.",
        )
    if file.size is not None and file.size > settings.max_upload_bytes:
        await file.close()
        raise _upload_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "upload_too_large",
            f"Uploads may not exceed {settings.max_upload_bytes} bytes.",
        )

    original_filename = _normalize_filename(file.filename)
    declared_content_type = _normalize_declared_content_type(file.content_type)
    extension = Path(original_filename).suffix.lower()
    extension_content_type = _EXTENSION_CONTENT_TYPES.get(extension)
    if (
        declared_content_type not in allowed_content_types
        or extension_content_type != declared_content_type
    ):
        raise _upload_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            "Filename extension and declared MIME type must identify an allowed JPG, PNG, or PDF.",
        )

    upload_root = settings.upload_dir.expanduser().resolve()
    try:
        if upload_root.exists():
            if not upload_root.is_dir() or stat.S_IMODE(upload_root.stat().st_mode) & 0o077:
                raise OSError("upload root must be a private directory")
        else:
            upload_root.mkdir(mode=0o700, parents=True)
    except OSError as exc:
        await file.close()
        raise _upload_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "upload_storage_error",
            "The upload storage directory is unavailable.",
        ) from exc
    owner_bucket = hashlib.sha256(user.id.encode()).hexdigest()[:32]
    now = datetime.now(timezone.utc)
    storage_key = Path(
        owner_bucket,
        f"{now.year:04d}",
        f"{now.month:02d}",
        f"{uuid.uuid4()}{_CANONICAL_EXTENSIONS[declared_content_type]}",
    )
    final_path = (upload_root / storage_key).resolve()
    if upload_root not in final_path.parents:
        raise _upload_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "unsafe_upload_path",
            "The upload storage path is not safe.",
        )

    temp_path = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.part")
    final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(final_path.parent, 0o700)

    total = 0
    digest = hashlib.sha256()
    head = b""
    tail = b""
    recorded = False
    try:
        with _open_private_temp(temp_path) as output:
            while chunk := await file.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise _upload_error(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "upload_too_large",
                        f"Uploads may not exceed {settings.max_upload_bytes} bytes.",
                    )
                digest.update(chunk)
                if len(head) < 16:
                    head = (head + chunk)[:16]
                tail = (tail + chunk)[-2048:]
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if total == 0:
            raise _upload_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "empty_upload",
                "The uploaded file is empty.",
            )
        detected_content_type = _detected_content_type(head, tail)
        if detected_content_type != declared_content_type:
            raise _upload_error(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "file_signature_mismatch",
                "The file signature does not match its MIME type.",
            )

        os.replace(temp_path, final_path)
        os.chmod(final_path, 0o600)
        try:
            response = owner_service.create_media_asset_record(
                db,
                user,
                purpose=purpose,
                storage_key=storage_key.as_posix(),
                original_filename=original_filename,
                content_type=detected_content_type,
                size_bytes=total,
                sha256=digest.hexdigest(),
            )
        except Exception:
            db.rollback()
            raise
        recorded = True
        return response
    except HTTPException:
        raise
    except OSError as exc:
        db.rollback()
        raise _upload_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "upload_storage_error",
            "The upload could not be stored.",
        ) from exc
    finally:
        await file.close()
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        if not recorded and final_path.exists():
            final_path.unlink(missing_ok=True)
