"""Kiểm tra đường dẫn, đặt tên thư mục và tính SHA-256 của tệp."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

from relay.transfer_types import CHUNK_SIZE, UnsafePathError

INVALID_WINDOWS_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


def sanitize_relative_path(value: str) -> Path:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise UnsafePathError("The selected path is not safe to transfer.")
    safe_parts: list[str] = []
    for part in path.parts:
        if part in {"", ".", ".."}:
            raise UnsafePathError("The selected path is not safe to transfer.")
        cleaned = INVALID_WINDOWS_CHARACTERS.sub("_", part).rstrip(" .")
        if not cleaned:
            cleaned = "_"
        if cleaned.upper() in RESERVED_WINDOWS_NAMES:
            cleaned = f"_{cleaned}"
        safe_parts.append(cleaned[:240])
    if not safe_parts:
        raise UnsafePathError("The selected path is not safe to transfer.")
    return Path(*safe_parts)

def safe_join(root: Path, relative_path: str) -> Path:
    relative = sanitize_relative_path(relative_path)
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise UnsafePathError("The selected path is not safe to transfer.")
    return candidate

def device_folder_name(name: str) -> str:
    cleaned = INVALID_WINDOWS_CHARACTERS.sub("_", name).strip(" .")[:80].rstrip(" .")
    if not cleaned:
        cleaned = "Relay device"
    if cleaned.split(".")[0].upper() in RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()
