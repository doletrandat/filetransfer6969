"""API mời điện thoại, quản lý phiên và gửi hoặc tải tệp."""
from __future__ import annotations

import base64
import io
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

import qrcode
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from qrcode.image.pil import PilImage
from starlette.requests import ClientDisconnect

from relay.file_preview import received_file_response
from relay.phone import (
    PHONE_COOKIE_NAME,
    PHONE_SESSION_TTL_SECONDS,
    PhoneSession,
    detect_phone_name,
)
from relay.transfer_types import TransferCancelled, TransferError

if TYPE_CHECKING:
    from relay.app import AppContext


def build_phone_router(
    context: AppContext, templates: Jinja2Templates, static_version: str,
) -> APIRouter:
    router = APIRouter()

    def require_phone(request: Request) -> PhoneSession:
        session = context.phone.get_session(request.cookies.get(PHONE_COOKIE_NAME))
        if session is None:
            raise HTTPException(status_code=401, detail="Open a new phone link on the computer.")
        return session

    @router.post("/api/v1/phone/invite")
    def create_phone_invitation() -> dict[str, Any]:
        if context.advertised_address == "127.0.0.1":
            raise HTTPException(status_code=503, detail="No local network address is available.")
        token, expires_at = context.phone.create_invitation()
        url = f"https://{context.advertised_address}:{context.port}/phone?invite={quote(token)}"
        image = qrcode.make(url, image_factory=PilImage)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return {
            "url": url,
            "expires_at": expires_at,
            "qr_data_url": (
                "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
            ),
        }

    @router.post("/api/v1/phone/revoke")
    def revoke_phone() -> dict[str, bool]:
        context.phone.revoke()
        return {"disconnected": True}

    @router.get("/phone", response_class=HTMLResponse)
    def phone_page(request: Request, invite: str | None = None) -> Any:
        if invite:
            if not context.phone.valid_invitation(invite):
                raise HTTPException(
                    status_code=403, detail="This phone link has expired or was used."
                )
            return templates.TemplateResponse(
                request=request,
                name="phone-connect.html",
                context={"invite": invite, "device_name": context.identity.name,
                         "static_version": static_version},
            )
        session = require_phone(request)
        return templates.TemplateResponse(
            request=request,
            name="phone.html",
            context={"phone_token": session.csrf_token, "device_name": context.identity.name,
                     "static_version": static_version},
        )

    @router.post("/phone/connect")
    def connect_phone(
        request: Request,
        invite: Annotated[str, Form(min_length=20, max_length=100)],
        device_model: Annotated[str, Form(max_length=80)] = "",
    ) -> RedirectResponse:
        device_name = detect_phone_name(
            request.headers.get("user-agent", ""), device_model,
            request.headers.get("sec-ch-ua-model", ""),
        )
        redeemed = context.phone.redeem(invite, device_name)
        if redeemed is None:
            raise HTTPException(status_code=403, detail="This phone link has expired or was used.")
        cookie, _ = redeemed
        response = RedirectResponse("/phone", status_code=303)
        response.set_cookie(
            PHONE_COOKIE_NAME,
            cookie,
            max_age=PHONE_SESSION_TTL_SECONDS,
            path="/phone",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.get("/phone/api/state")
    def phone_state(session: PhoneSession = Depends(require_phone)) -> dict[str, Any]:
        return {
            "available": [
                {"id": item.id, "name": item.relative_path, "size": item.size}
                for item in context.transfers.list_staged()
            ],
            "uploaded": context.phone.uploaded_files(session),
            "expires_at": session.expires_at,
        }

    @router.delete("/phone/api/uploads/{upload_id}")
    def cancel_phone_upload(
        upload_id: str,
        request: Request,
        session: PhoneSession = Depends(require_phone),
    ) -> dict[str, Any]:
        if not secrets.compare_digest(
            request.headers.get("X-Relay-Phone-Token", ""), session.csrf_token
        ):
            raise HTTPException(status_code=403, detail="The phone session could not be verified.")
        try:
            return context.phone.cancel_upload(upload_id, session).public_dict()
        except TransferError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/phone/api/files")
    async def phone_upload(
        request: Request,
        filename: Annotated[str, Query(min_length=1, max_length=255)],
        upload_id: Annotated[str | None, Query(min_length=16, max_length=64)] = None,
        total_size: Annotated[int | None, Query(ge=0)] = None,
        session: PhoneSession = Depends(require_phone),
    ) -> dict[str, Any]:
        if not secrets.compare_digest(
            request.headers.get("X-Relay-Phone-Token", ""), session.csrf_token
        ):
            raise HTTPException(status_code=403, detail="The phone session could not be verified.")
        try:
            return await context.phone.receive_file(
                session,
                context.settings.load().destination,
                filename,
                request.stream(),
                upload_id=upload_id,
                total_size=total_size,
            )
        except ClientDisconnect:
            raise HTTPException(status_code=499, detail="The phone disconnected.") from None
        except TransferCancelled as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (TransferError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/phone/api/files/{item_id}", response_class=FileResponse)
    def phone_download(
        item_id: str, session: PhoneSession = Depends(require_phone), preview: bool = False
    ) -> FileResponse:
        item = next(
            (candidate for candidate in context.transfers.list_staged() if candidate.id == item_id),
            None,
        )
        if item is None or not item.path.is_file():
            raise HTTPException(status_code=404, detail="This file is no longer available.")
        if preview:
            return received_file_response(item.path, Path(item.relative_path).name)
        return FileResponse(item.path, filename=Path(item.relative_path).name)

    return router
