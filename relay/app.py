from __future__ import annotations

import base64
import io
import ipaddress
import os
import secrets
import socket
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import qrcode
from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from qrcode.image.pil import PilImage
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from relay.config import SettingsStore, get_local_addresses, load_or_create_identity
from relay.discovery import DiscoveryManager
from relay.models import IncomingManifestRequest
from relay.network import PeerConnectionError
from relay.phone import PHONE_COOKIE_NAME, PHONE_SESSION_TTL_SECONDS, PhoneAccess, PhoneSession
from relay.security import AuthorizedPeer, PairingError, PairingRegistry, SessionRegistry
from relay.transfers import TransferError, TransferManager

PACKAGE_DIR = Path(__file__).resolve().parent
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


def open_with_default_app(path: Path) -> None:
    if os.name != "nt":
        raise OSError("Opening received files is supported on Windows only.")
    os.startfile(str(path))


class CodeRequest(BaseModel):
    code: str = Field(min_length=8, max_length=8)
    endpoint: str | None = Field(default=None, max_length=300)
    fingerprint: str | None = Field(default=None, min_length=47, max_length=95)


class AppContext:
    def __init__(self, data_dir: Path, port: int) -> None:
        self.data_dir = data_dir
        self.port = port
        self.control_token = secrets.token_urlsafe(32)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = SettingsStore(data_dir)
        self.settings.load()
        self.addresses = get_local_addresses()
        self.advertised_address = next(
            (address for address in self.addresses if not address.startswith("127.")),
            "127.0.0.1",
        )
        device_name = socket.gethostname().split(".")[0][:80] or "Relay device"
        self.identity = load_or_create_identity(
            self.data_dir,
            device_name,
            self.addresses,
            port,
        )
        self.pairing = PairingRegistry()
        self.sessions = SessionRegistry()
        self.phone = PhoneAccess(data_dir)
        self.discovery = DiscoveryManager(
            device_id=self.identity.id,
            device_name=self.identity.name,
            host=self.advertised_address,
            port=port,
            fingerprint=self.identity.fingerprint,
            addresses=self.addresses,
        )
        self.transfers = TransferManager(
            self.data_dir,
            self.settings,
            self.identity,
            self.discovery,
        )

    def start(self) -> None:
        try:
            self.discovery.start(self.advertised_address)
        except Exception as error:
            print(f"Device discovery is unavailable: {error}")

    def stop(self) -> None:
        self.discovery.stop()


def create_app(context: AppContext) -> FastAPI:
    app = FastAPI(title="Relay", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.context = context
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    static_version = str(max(
        (PACKAGE_DIR / "static" / name).stat().st_mtime_ns
        for name in ("app.js", "styles.css", "phone.js", "phone.css")
    ))

    def require_session(authorization: Annotated[str | None, Header()] = None) -> AuthorizedPeer:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401, detail="Pair with this device before transferring."
            )
        peer = context.sessions.verify(authorization.removeprefix("Bearer "))
        if peer is None:
            raise HTTPException(status_code=401, detail="This pairing has expired. Pair again.")
        return peer

    def require_phone(request: Request) -> PhoneSession:
        session = context.phone.get_session(request.cookies.get(PHONE_COOKIE_NAME))
        if session is None:
            raise HTTPException(status_code=401, detail="Open a new phone link on the computer.")
        return session

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        local_route = path == "/" or (
            path.startswith("/api/v1/") and not path.startswith("/api/v1/remote/")
        )
        phone_route = path == "/phone" or path.startswith("/phone/")
        if phone_route and (
            request.url.scheme != "https"
            or request.url.hostname not in context.addresses
            or request.url.hostname == "127.0.0.1"
        ):
            return Response(status_code=403)
        if local_route:
            client_host = request.client.host if request.client else ""
            try:
                local_client = ipaddress.ip_address(client_host).is_loopback
            except ValueError:
                local_client = False
            if not local_client or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
                return Response(status_code=403)
            if path.startswith("/api/v1/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get("X-Relay-Control-Token", "")
                if not secrets.compare_digest(supplied, context.control_token):
                    return Response(status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        ))
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if local_route or phone_route:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        return templates.TemplateResponse(
            request=request, name="index.html",
            context={"control_token": context.control_token, "static_version": static_version},
        )

    @app.post("/api/v1/phone/invite")
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

    @app.post("/api/v1/phone/revoke")
    def revoke_phone() -> dict[str, bool]:
        context.phone.revoke()
        return {"disconnected": True}

    @app.get("/phone", response_class=HTMLResponse)
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

    @app.post("/phone/connect")
    def connect_phone(
        invite: Annotated[str, Form(min_length=20, max_length=100)],
    ) -> RedirectResponse:
        redeemed = context.phone.redeem(invite)
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

    @app.get("/phone/api/state")
    def phone_state(session: PhoneSession = Depends(require_phone)) -> dict[str, Any]:
        return {
            "available": [
                {"id": item.id, "name": item.relative_path, "size": item.size}
                for item in context.transfers.list_staged()
            ],
            "uploaded": context.phone.uploaded_files(session),
            "expires_at": session.expires_at,
        }

    @app.post("/phone/api/files")
    async def phone_upload(
        request: Request,
        filename: Annotated[str, Query(min_length=1, max_length=255)],
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
            )
        except ClientDisconnect:
            raise HTTPException(status_code=499, detail="The phone disconnected.") from None
        except (TransferError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/phone/api/files/{item_id}", response_class=FileResponse)
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

    @app.get("/api/v1/status")
    def status() -> dict[str, Any]:
        settings = context.settings.load()
        ticket = context.pairing.ticket
        return {
            "device": {
                "id": context.identity.id,
                "name": context.identity.name,
                "fingerprint": context.identity.fingerprint,
            },
            "addresses": context.addresses,
            "destination": str(settings.destination),
            "pairing_code": ticket.code if ticket else None,
            "pairing_expires_at": ticket.expires_at if ticket else None,
        }

    @app.put("/api/v1/settings")
    def update_settings(payload: dict[str, str]) -> dict[str, str]:
        destination = payload.get("destination", "")
        if not destination:
            raise HTTPException(status_code=422, detail="Choose a destination folder.")
        try:
            settings = context.settings.update(destination)
        except OSError as error:
            raise HTTPException(
                status_code=400,
                detail=f"Relay could not use that folder: {error}",
            ) from error
        return {"destination": str(settings.destination)}

    @app.get("/api/v1/devices")
    def devices() -> list[dict[str, Any]]:
        return [device.public_dict() for device in context.discovery.list_devices()]

    @app.post("/api/v1/pairing/code")
    def create_pairing_code() -> dict[str, Any]:
        ticket = context.pairing.create_ticket()
        context.discovery.update_code_hash(ticket.code_hash, context.advertised_address)
        endpoint = f"https://{context.advertised_address}:{context.port}"
        payload = (
            f"relay://pair?endpoint={quote(endpoint)}&code={quote(ticket.code)}"
            f"&fp={quote(context.identity.fingerprint)}"
        )
        image = qrcode.make(payload, image_factory=PilImage)
        output = io.BytesIO()
        image.save(output, format="PNG")
        qr_data_url = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        return {
            "code": ticket.code,
            "expires_at": ticket.expires_at,
            "fingerprint": context.identity.fingerprint,
            "endpoint": endpoint,
            "qr_data_url": qr_data_url,
        }

    @app.post("/api/v1/pair")
    def pair(payload: CodeRequest) -> dict[str, Any]:
        try:
            session = context.transfers.pair(
                payload.code,
                endpoint=payload.endpoint,
                fingerprint=payload.fingerprint,
            )
        except (TransferError, PeerConnectionError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return session.public_dict()

    @app.get("/api/v1/state")
    def state_snapshot() -> dict[str, Any]:
        return {
            "status": status(),
            "devices": devices(),
            "peers": peers(),
            "staged": staged(),
            "outgoing": outgoing_transfers(),
            "incoming": incoming_transfers(),
            "phone_uploads": context.phone.recent_uploads(),
            "phone_connection": context.phone.connection_state(),
        }

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
            open_with_default_app(path)
        except OSError as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Windows không mở được tệp. "
                    "Hãy kiểm tra ứng dụng mặc định cho loại tệp này."
                ),
            ) from error
        return {"opened": True}

    @app.post("/api/v1/received/phone/{upload_id}/open")
    def open_phone_upload_in_windows(upload_id: str) -> dict[str, bool]:
        return open_received_file(phone_upload_path(upload_id))

    @app.post("/api/v1/received/computer/{transfer_id}/{item_id}/open")
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

    @app.get("/api/v1/received/phone/{upload_id}/preview", response_class=HTMLResponse)
    def preview_phone_upload(request: Request, upload_id: str) -> HTMLResponse:
        path = phone_upload_path(upload_id)
        return preview_response(request, path, f"/api/v1/received/phone/{quote(upload_id)}")

    @app.get("/api/v1/received/phone/{upload_id}", response_class=FileResponse)
    def open_phone_upload(upload_id: str, download: bool = False) -> FileResponse:
        return received_file_response(phone_upload_path(upload_id), download=download)

    @app.get(
        "/api/v1/received/computer/{transfer_id}/{item_id}/preview",
        response_class=HTMLResponse,
    )
    def preview_computer_upload(request: Request, transfer_id: str, item_id: str) -> HTMLResponse:
        path = computer_upload_path(transfer_id, item_id)
        file_url = (
            f"/api/v1/received/computer/{quote(transfer_id)}/{quote(item_id)}"
        )
        return preview_response(request, path, file_url)

    @app.get(
        "/api/v1/received/computer/{transfer_id}/{item_id}", response_class=FileResponse
    )
    def open_computer_upload(
        transfer_id: str, item_id: str, download: bool = False
    ) -> FileResponse:
        return received_file_response(
            computer_upload_path(transfer_id, item_id), download=download
        )

    @app.get("/api/v1/peers")
    def peers() -> list[dict[str, Any]]:
        return [peer.public_dict() for peer in context.transfers.list_peers()]

    @app.get("/api/v1/staged")
    def staged() -> list[dict[str, Any]]:
        return [item.public_dict() for item in context.transfers.list_staged()]

    @app.post("/api/v1/stage", response_model=None)
    async def stage(
        request: Request,
        relative_path: Annotated[str, Query(min_length=1, max_length=1024)],
    ) -> dict[str, Any] | Response:
        try:
            item = await context.transfers.stage(relative_path, request.stream())
        except ClientDisconnect:
            return Response(status_code=499)
        except (TransferError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return item.public_dict()

    @app.post("/api/v1/stage/chunk", response_model=None)
    async def stage_chunk(
        request: Request,
        upload_id: Annotated[str, Query(min_length=16, max_length=64)],
        relative_path: Annotated[str, Query(min_length=1, max_length=1024)],
        total_size: Annotated[int, Query(ge=0)],
        offset: Annotated[int, Query(ge=0)],
        final: bool = False,
    ) -> dict[str, Any] | Response:
        try:
            return await context.transfers.stage_chunk(
                upload_id=upload_id,
                relative_path=relative_path,
                total_size=total_size,
                offset=offset,
                final=final,
                stream=request.stream(),
            )
        except ClientDisconnect:
            return Response(status_code=499)
        except (TransferError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/v1/staged/{item_id}")
    def remove_staged(item_id: str) -> dict[str, bool]:
        return {"removed": context.transfers.remove_staged(item_id)}

    @app.delete("/api/v1/staged")
    def clear_staged() -> dict[str, int]:
        return {"removed": context.transfers.clear_staged()}

    @app.post("/api/v1/transfers")
    def start_transfer(
        payload: dict[str, Any],
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            transfer = context.transfers.start_outgoing(
                str(payload.get("peer_id", "")),
                str(payload.get("batch_name", "Relay transfer")),
                [str(item_id) for item_id in payload.get("item_ids", [])],
            )
        except (TransferError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        background_tasks.add_task(context.transfers.run_outgoing, transfer.id)
        return transfer.public_dict()

    @app.post("/api/v1/transfers/{transfer_id}/retry")
    def retry_transfer(transfer_id: str) -> dict[str, Any]:
        try:
            transfer = context.transfers.retry_outgoing(transfer_id)
        except TransferError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return transfer.public_dict()

    @app.get("/api/v1/transfers/outgoing")
    def outgoing_transfers() -> list[dict[str, Any]]:
        return [transfer.public_dict() for transfer in context.transfers.list_outgoing()]

    @app.get("/api/v1/transfers/incoming")
    def incoming_transfers() -> list[dict[str, Any]]:
        return [transfer.public_dict() for transfer in context.transfers.list_incoming()]

    @app.get("/api/v1/remote/identity")
    def remote_identity() -> dict[str, str]:
        return {
            "id": context.identity.id,
            "name": context.identity.name,
            "fingerprint": context.identity.fingerprint,
        }

    @app.post("/api/v1/remote/pair")
    def remote_pair(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            device = AuthorizedPeer(
                id=str(payload["device"]["id"]),
                name=str(payload["device"]["name"]),
                fingerprint=str(payload["device"]["fingerprint"]),
            )
            code = str(payload["code"])
            token = context.pairing.redeem(code, device)
        except (KeyError, PairingError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        context.sessions.register(token, device)
        return {
            "session_token": token,
            "device": {
                "id": context.identity.id,
                "name": context.identity.name,
                "fingerprint": context.identity.fingerprint,
            },
            "expires_in": 3600,
        }

    @app.post("/api/v1/remote/transfers")
    def remote_start_transfer(
        payload: dict[str, Any],
        peer: AuthorizedPeer = Depends(require_session),
    ) -> dict[str, Any]:
        try:
            request_model = IncomingManifestRequest.model_validate(payload)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail="The transfer manifest is invalid."
            ) from error
        if (
            request_model.source.id != peer.id
            or request_model.source.fingerprint.upper() != peer.fingerprint.upper()
        ):
            raise HTTPException(status_code=403, detail="The paired device identity did not match.")
        try:
            return context.transfers.create_incoming(request_model).model_dump()
        except TransferError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/remote/transfers/{transfer_id}/files/{item_id}")
    async def remote_receive_file(
        transfer_id: str,
        item_id: str,
        request: Request,
        peer: AuthorizedPeer = Depends(require_session),
    ) -> dict[str, str]:
        try:
            context.transfers.require_incoming_peer(transfer_id, peer.id, peer.fingerprint)
            await context.transfers.receive_file(transfer_id, item_id, request.stream())
        except TransferError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"received": "true"}

    @app.post(
        "/api/v1/remote/transfers/{transfer_id}/files/{item_id}/chunks",
        response_model=None,
    )
    async def remote_receive_chunk(
        transfer_id: str,
        item_id: str,
        request: Request,
        offset: Annotated[int, Query(ge=0)],
        total_size: Annotated[int, Query(ge=0)],
        final: bool,
        peer: AuthorizedPeer = Depends(require_session),
    ) -> dict[str, Any] | Response:
        try:
            context.transfers.require_incoming_peer(transfer_id, peer.id, peer.fingerprint)
            return await context.transfers.receive_chunk(
                transfer_id=transfer_id,
                item_id=item_id,
                offset=offset,
                total_size=total_size,
                final=final,
                stream=request.stream(),
            )
        except ClientDisconnect:
            return Response(status_code=499)
        except (TransferError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/remote/transfers/{transfer_id}")
    def remote_transfer_status(
        transfer_id: str,
        peer: AuthorizedPeer = Depends(require_session),
    ) -> dict[str, Any]:
        try:
            context.transfers.require_incoming_peer(transfer_id, peer.id, peer.fingerprint)
            return context.transfers.incoming_status(transfer_id).public_dict()
        except TransferError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    return app


def build_app(data_dir: Path, port: int) -> FastAPI:
    return create_app(AppContext(data_dir, port))
