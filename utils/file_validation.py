"""Input validation: file names, types (by magic bytes) and size limits."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePath

from config.settings import SETTINGS


class FileValidationError(ValueError):
    """Raised when an uploaded file is rejected."""


_MAGIC = {
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".tif": [b"II*\x00", b"MM\x00*"],
    ".tiff": [b"II*\x00", b"MM\x00*"],
    ".bmp": [b"BM"],
    ".webp": [b"RIFF"],
}


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Return a safe display name: no directories, no control or shell-special characters."""
    name = unicodedata.normalize("NFKC", name or "")
    # Strip any directory components (both separators) to defeat path traversal.
    name = PurePath(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")
    name = re.sub(r"\.{2,}", ".", name)
    if not name:
        name = "document"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: max_len - len(ext) - 1] + "." + ext) if dot else name[:max_len]
    return name


def file_extension(name: str) -> str:
    """Lower-case extension including the dot ('' if none)."""
    return PurePath(name).suffix.lower()


def validate_upload(name: str, data: bytes) -> tuple[str, str]:
    """Validate an upload. Returns (safe_name, kind) where kind is 'pdf' or 'image'.

    Raises FileValidationError with a user-facing message on rejection.
    """
    safe = sanitize_filename(name)
    ext = file_extension(safe)
    if ext not in SETTINGS.allowed_extensions:
        raise FileValidationError(
            f"Unsupported file type '{ext or 'none'}'. Allowed: {', '.join(SETTINGS.allowed_extensions)}"
        )
    if not data:
        raise FileValidationError("The file is empty.")
    max_bytes = SETTINGS.max_file_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise FileValidationError(f"File is {len(data) / 1e6:.1f} MB; the limit is {SETTINGS.max_file_mb} MB.")
    if ext == ".pdf":
        # The PDF header may be preceded by a little junk; readers accept it within the first 1 KB.
        if b"%PDF-" not in data[:1024]:
            raise FileValidationError("The file does not look like a PDF (missing %PDF header).")
        return safe, "pdf"
    if not any(data.startswith(m) for m in _MAGIC[ext]):
        raise FileValidationError(f"File content does not match its '{ext}' extension.")
    if ext == ".webp" and data[8:12] != b"WEBP":
        raise FileValidationError("File content does not match its '.webp' extension.")
    return safe, "image"


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 digest used to identify a document without storing it."""
    return hashlib.sha256(data).hexdigest()
