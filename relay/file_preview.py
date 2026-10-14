"""Chọn kiểu nội dung an toàn khi mở hoặc tải tệp đã nhận."""
from pathlib import Path

from fastapi.responses import FileResponse

PREVIEW_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
TEXT_PREVIEW_SUFFIXES = {
    ".txt", ".log", ".md", ".csv", ".json", ".html", ".htm", ".svg",
    ".xml", ".py", ".js", ".css", ".yaml", ".yml",
}


def received_file_response(
    path: Path, filename: str | None = None, *, download: bool = False
) -> FileResponse:
    name = filename or path.name
    suffix = Path(name).suffix.lower()
    media_type = PREVIEW_MEDIA_TYPES.get(suffix)
    content_security_policy = (
        "default-src 'none'; img-src 'self' data: blob:; media-src 'self' blob:"
        if media_type else "sandbox; default-src 'none'"
    )
    if suffix in TEXT_PREVIEW_SUFFIXES:
        media_type = "text/plain; charset=utf-8"
    return FileResponse(
        path,
        filename=name,
        media_type=media_type or "application/octet-stream",
        content_disposition_type="inline" if media_type and not download else "attachment",
        headers={"Content-Security-Policy": content_security_policy},
    )
