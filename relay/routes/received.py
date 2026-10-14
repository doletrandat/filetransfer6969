"""API mở, xem trước và tải tệp đã nhận từ máy tính hoặc điện thoại."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from relay.file_preview import TEXT_PREVIEW_SUFFIXES, received_file_response
from relay.transfer_types import TransferError

if TYPE_CHECKING:
    from relay.app import AppContext


def build_received_router(
    context: AppContext, templates: Jinja2Templates, open_file: Callable[[Path], None],
) -> APIRouter:
    router = APIRouter()

    def phone_upload_path(upload_id: str) -> Path:
        upload = next(
            (item for item in context.phone.recent_uploads() if item["id"] == upload_id),
            None,
        )
        if upload is None:
            raise HTTPException(status_code=404, detail="This file is no longer available.")
        path = Path(str(upload["path"]))
        if path.is_symlink() or not path.is_file():
            raise HTTPException(status_code=404, detail="This file is no longer available.")
        return path

    def computer_upload_path(transfer_id: str, item_id: str) -> Path:
        try:
            transfer = context.transfers.incoming_status(transfer_id)
        except TransferError as error:
            raise HTTPException(
                status_code=404, detail="This file is no longer available."
            ) from error
        item = next((candidate for candidate in transfer.items if candidate.id == item_id), None)
        if item is None or not item.completed:
            raise HTTPException(status_code=404, detail="This file is no longer available.")
        path = item.target
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(transfer.destination.resolve())
        ):
            raise HTTPException(status_code=404, detail="This file is no longer available.")
        return path

    def open_received_file(path: Path) -> dict[str, bool]:
        try:
            open_file(path)
        except OSError as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Windows không mở được tệp. "
                    "Hãy kiểm tra ứng dụng mặc định cho loại tệp này."
                ),
            ) from error
        return {"opened": True}

    @router.post("/api/v1/received/phone/{upload_id}/open")
    def open_phone_upload_in_windows(upload_id: str) -> dict[str, bool]:
        return open_received_file(phone_upload_path(upload_id))

    @router.post("/api/v1/received/computer/{transfer_id}/{item_id}/open")
    def open_computer_upload_in_windows(transfer_id: str, item_id: str) -> dict[str, bool]:
        return open_received_file(computer_upload_path(transfer_id, item_id))

    def preview_response(request: Request, path: Path, file_url: str) -> HTMLResponse:
        suffix = path.suffix.lower()
        kind = (
            "image" if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
            else "video" if suffix in {".mp4", ".webm"}
            else "audio" if suffix in {".mp3", ".ogg"}
            else "text" if suffix in TEXT_PREVIEW_SUFFIXES
            else "pdf" if suffix == ".pdf"
            else "other"
        )
        preview_text = ""
        truncated = False
        if kind == "text":
            try:
                with path.open("rb") as source:
                    content = source.read(256 * 1024 + 1)
            except OSError as error:
                raise HTTPException(
                    status_code=404, detail="This file is no longer available."
                ) from error
            truncated = len(content) > 256 * 1024
            preview_text = content[:256 * 1024].decode("utf-8", errors="replace")
        return templates.TemplateResponse(
            request=request,
            name="preview.html",
            context={
                "filename": path.name, "kind": kind, "file_url": file_url,
                "preview_text": preview_text, "truncated": truncated,
            },
        )

    @router.get("/api/v1/received/phone/{upload_id}/preview", response_class=HTMLResponse)
    def preview_phone_upload(request: Request, upload_id: str) -> HTMLResponse:
        path = phone_upload_path(upload_id)
        return preview_response(request, path, f"/api/v1/received/phone/{quote(upload_id)}")

    @router.get("/api/v1/received/phone/{upload_id}", response_class=FileResponse)
    def open_phone_upload(upload_id: str, download: bool = False) -> FileResponse:
        return received_file_response(phone_upload_path(upload_id), download=download)

    @router.get(
        "/api/v1/received/computer/{transfer_id}/{item_id}/preview",
        response_class=HTMLResponse,
    )
    def preview_computer_upload(request: Request, transfer_id: str, item_id: str) -> HTMLResponse:
        path = computer_upload_path(transfer_id, item_id)
        file_url = (
            f"/api/v1/received/computer/{quote(transfer_id)}/{quote(item_id)}"
        )
        return preview_response(request, path, file_url)

    @router.get(
        "/api/v1/received/computer/{transfer_id}/{item_id}", response_class=FileResponse
    )
    def open_computer_upload(
        transfer_id: str, item_id: str, download: bool = False
    ) -> FileResponse:
        return received_file_response(
            computer_upload_path(transfer_id, item_id), download=download
        )

    return router
