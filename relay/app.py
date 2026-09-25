from __future__ import annotations

import base64
import io
import socket
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import qrcode
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
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
from relay.security import AuthorizedPeer, PairingError, PairingRegistry, SessionRegistry
from relay.transfers import TransferError, TransferManager

PACKAGE_DIR = Path(__file__).resolve().parent


class CodeRequest(BaseModel):
    code: str = Field(min_length=8, max_length=8)
    endpoint: str | None = Field(default=None, max_length=300)
    fingerprint: str | None = Field(default=None, min_length=47, max_length=95)


class AppContext:
    def __init__(self, data_dir: Path, port: int) -> None:
        self.data_dir = data_dir
        self.port = port
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

    def require_session(authorization: Annotated[str | None, Header()] = None) -> AuthorizedPeer:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401, detail="Pair with this device before transferring."
            )
        peer = context.sessions.verify(authorization.removeprefix("Bearer "))
        if peer is None:
            raise HTTPException(status_code=401, detail="This pairing has expired. Pair again.")
        return peer

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        return templates.TemplateResponse(request=request, name="index.html")

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
        }

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
        del peer
        try:
            request_model = IncomingManifestRequest.model_validate(payload)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail="The transfer manifest is invalid."
            ) from error
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
        del peer
        try:
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
        del peer
        try:
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
        del peer
        try:
            return context.transfers.incoming_status(transfer_id).public_dict()
        except TransferError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    return app


def build_app(data_dir: Path, port: int) -> FastAPI:
    return create_app(AppContext(data_dir, port))
