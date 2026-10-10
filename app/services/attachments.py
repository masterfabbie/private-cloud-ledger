"""Receipts and other files attached to transactions.

Files are stored on disk in <attachments_dir>/<user_id>/<random name>. Photos are rotated
according to their EXIF orientation, downscaled and re-encoded as JPEG, which also removes
all metadata such as GPS coordinates. PDFs are stored unchanged.
"""

import io
import os
import secrets
import time
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings

MAX_IMAGE_SIDE = 2400
THUMB_SIDE = 360
MAX_PER_TRANSACTION = 20


class AttachmentError(ValueError):
    pass


def storage_dir() -> Path:
    s = get_settings()
    if s.attachments_dir:
        return Path(s.attachments_dir)
    url = s.database_url
    if url.startswith("sqlite:///") and ":memory:" not in url:
        return Path(url.removeprefix("sqlite:///")).resolve().parent / "attachments"
    return Path("data/attachments").resolve()


def _user_dir(user_id: int) -> Path:
    return storage_dir() / str(user_id)


def file_path(att: models.Attachment, thumb: bool = False) -> Path:
    name = att.stored_name + (".thumb.jpg" if thumb else "")
    return _user_dir(att.user_id) / name


def sniff(data: bytes) -> str | None:
    """Detect the file type from its first bytes (never trust the browser's content type)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "image/heic"
    return None


def _jpeg(img: Image.Image, side: int) -> bytes:
    img = img.copy()
    img.thumbnail((side, side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)  # no exif= argument: metadata is dropped
    return out.getvalue()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _clean_name(name: str) -> str:
    name = os.path.basename(name or "").strip().replace("\x00", "")
    return name[:200] or "receipt"


def store(db: Session, tx: models.Transaction, filename: str, data: bytes) -> models.Attachment:
    if len(data) > get_settings().max_attachment_mb * 1024 * 1024:
        raise AttachmentError(f"{filename}: the file is larger than {get_settings().max_attachment_mb} MB.")
    if len(tx.attachments) >= MAX_PER_TRANSACTION:
        raise AttachmentError(f"A transaction can have at most {MAX_PER_TRANSACTION} attachments.")
    kind = sniff(data)
    if kind is None:
        raise AttachmentError(f"{filename}: only photos (JPEG, PNG, WebP) and PDF files can be attached.")
    if kind == "image/heic":
        raise AttachmentError(
            f"{filename}: HEIC photos are not supported. Take the photo directly in Proud Ledger, "
            "or set the iPhone camera to “Most Compatible” (JPEG)."
        )

    stored = secrets.token_hex(16)
    name = _clean_name(filename)
    thumb = None
    if kind == "application/pdf":
        content, content_type = data, "application/pdf"
    else:
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                img = ImageOps.exif_transpose(img).convert("RGB")
                content, thumb = _jpeg(img, MAX_IMAGE_SIDE), _jpeg(img, THUMB_SIDE)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AttachmentError(f"{filename}: the image could not be read.") from exc
        content_type = "image/jpeg"
        if not name.lower().endswith((".jpg", ".jpeg")):
            name = os.path.splitext(name)[0] + ".jpg"

    att = models.Attachment(
        user_id=tx.user_id, transaction_id=tx.id, filename=name, content_type=content_type,
        size=len(content), stored_name=stored, has_thumb=thumb is not None,
    )
    _write(file_path(att), content)
    if thumb is not None:
        _write(file_path(att, thumb=True), thumb)
    tx.attachments.append(att)
    return att


def remove_files(atts: list[tuple[int, str]]) -> None:
    """Delete stored files given (user_id, stored_name) pairs. Call after the DB commit."""
    for user_id, stored in atts:
        for suffix in ("", ".thumb.jpg"):
            try:
                (_user_dir(user_id) / (stored + suffix)).unlink()
            except FileNotFoundError:
                pass


def files_of(db: Session, *where) -> list[tuple[int, str]]:
    """(user_id, stored_name) of attachments whose transaction matches the conditions."""
    T, A = models.Transaction, models.Attachment
    return [tuple(r) for r in db.execute(select(A.user_id, A.stored_name).join(T, A.transaction_id == T.id).where(*where)).all()]


def cleanup_orphans(db: Session, min_age_seconds: int = 3600) -> int:
    """Delete files that no attachment row refers to (e.g. after a crash). Returns the count."""
    root = storage_dir()
    if not root.exists():
        return 0
    known = set(db.scalars(select(models.Attachment.stored_name)))
    cutoff = time.time() - min_age_seconds
    removed = 0
    for path in root.glob("*/*"):
        stored = path.name.split(".")[0]
        if stored not in known and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
