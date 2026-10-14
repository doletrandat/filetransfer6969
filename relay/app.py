"""Khởi tạo ứng dụng, bảo vệ API và phục vụ giao diện điều khiển."""
from __future__ import annotations

import ipaddress
import os
import secrets
import socket
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from relay.config import SettingsStore, get_local_addresses, load_or_create_identity
from relay.discovery import DiscoveryManager
from relay.network import PeerConnectionError
from relay.phone import PhoneAccess
from relay.routes.phone import build_phone_router
from relay.routes.received import build_received_router
from relay.routes.remote import build_remote_router
from relay.transfers import TransferError, TransferManager

PACKAGE_DIR = Path(__file__).resolve().parent


def open_with_default_app(path: Path) -> None:
    if os.name != "nt":
        raise OSError("Opening received files is supported on Windows only.")
    os.startfile(str(path))


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
        for name in ("app.js", "state.js", "ui.js", "render.js", "actions.js",
                     "styles.css", "phone.js", "phone.css")
    ))

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

    @app.get("/api/v1/status")
    def status() -> dict[str, Any]:
        settings = context.settings.load()
        return {
            "device": {
                "id": context.identity.id,
                "name": context.identity.name,
                "fingerprint": context.identity.fingerprint,
            },
            "addresses": context.addresses,
            "destination": str(settings.destination),
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

    @app.get("/api/v1/state")
    def state_snapshot() -> dict[str, Any]:
        return {
            "status": status(),
            "devices": devices(),
            "receiving_peers": [
                transfer.source.model_dump() for transfer in context.transfers.list_incoming()
                if transfer.status in {"receiving", "waiting"}
            ],
            "staged": staged(),
            "outgoing": outgoing_transfers(),
            "incoming": incoming_transfers(),
            "incoming_requests": [offer.public_dict() for offer in context.transfers.list_offers()],
            "phone_uploads": (
                context.phone.transfer_activity() + context.phone.recent_uploads()
            ),
            "phone_connection": context.phone.connection_state(),
        }

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
                retain_staged=payload.get("retain_staged") is True,
            )
        except (TransferError, PeerConnectionError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        background_tasks.add_task(context.transfers.run_outgoing, transfer.id)
        return transfer.public_dict()

    @app.post("/api/v1/transfers/{transfer_id}/retry")
    def retry_transfer(transfer_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            transfer = context.transfers.retry_outgoing(transfer_id)
        except (TransferError, PeerConnectionError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        background_tasks.add_task(context.transfers.run_outgoing, transfer.id)
        return transfer.public_dict()

    @app.delete("/api/v1/transfers/outgoing/{transfer_id}")
    def cancel_outgoing_transfer(transfer_id: str) -> dict[str, Any]:
        try:
            return context.transfers.cancel_outgoing(transfer_id).public_dict()
        except TransferError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.delete("/api/v1/transfers/incoming/{transfer_id}")
    def cancel_incoming_transfer(transfer_id: str) -> dict[str, Any]:
        try:
            return context.transfers.cancel_incoming(transfer_id).public_dict()
        except TransferError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.delete("/api/v1/phone/uploads/{upload_id}")
    def cancel_phone_upload_from_computer(upload_id: str) -> dict[str, Any]:
        try:
            return context.phone.cancel_upload(upload_id).public_dict()
        except TransferError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/transfers/outgoing")
    def outgoing_transfers() -> list[dict[str, Any]]:
        return [transfer.public_dict() for transfer in context.transfers.list_outgoing()]

    @app.get("/api/v1/transfers/incoming")
    def incoming_transfers() -> list[dict[str, Any]]:
        return [transfer.public_dict() for transfer in context.transfers.list_incoming()]

    app.include_router(build_phone_router(context, templates, static_version))
    app.include_router(build_received_router(
        context, templates, lambda path: open_with_default_app(path),
    ))
    app.include_router(build_remote_router(context))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    return app


def build_app(data_dir: Path, port: int) -> FastAPI:
    return create_app(AppContext(data_dir, port))
