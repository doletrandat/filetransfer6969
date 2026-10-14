"""API trao đổi giữa hai máy tính và quyết định nhận từng lượt gửi."""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from relay.models import IncomingManifestRequest
from relay.transfer_types import ReceiveOffer, TransferCancelled, TransferError

if TYPE_CHECKING:
    from relay.app import AppContext


def build_remote_router(context: AppContext) -> APIRouter:
    router = APIRouter()

    def require_offer(
        transfer_id: str, authorization: Annotated[str | None, Header()] = None,
    ) -> ReceiveOffer:
        try:
            return context.transfers.verify_offer(
                transfer_id, (authorization or "").removeprefix("Bearer "),
            )
        except TransferError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    def require_accepted(offer: ReceiveOffer = Depends(require_offer)) -> ReceiveOffer:
        if offer.status != "accepted":
            raise HTTPException(status_code=403, detail="Máy nhận chưa chấp nhận lượt gửi này.")
        return offer

    @router.get("/api/v1/remote/identity")
    def remote_identity() -> dict[str, str]:
        return {
            "id": context.identity.id,
            "name": context.identity.name,
            "fingerprint": context.identity.fingerprint,
        }

    @router.post("/api/v1/remote/transfers")
    def remote_start_transfer(payload: IncomingManifestRequest) -> dict[str, Any]:
        try:
            offer = context.transfers.request_incoming(payload)
            return {**offer.public_dict(), "token": offer.token}
        except TransferError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/v1/requests/{transfer_id}/accept")
    def accept_request(transfer_id: str) -> dict[str, Any]:
        try:
            return context.transfers.decide_offer(transfer_id, True).public_dict()
        except (TransferError, OSError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/api/v1/requests/{transfer_id}/reject")
    def reject_request(transfer_id: str) -> dict[str, Any]:
        try:
            return context.transfers.decide_offer(transfer_id, False).public_dict()
        except TransferError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/api/v1/remote/transfers/{transfer_id}/files/{item_id}")
    async def remote_receive_file(
        transfer_id: str,
        item_id: str,
        request: Request,
        offer: ReceiveOffer = Depends(require_accepted),
    ) -> dict[str, str]:
        try:
            await context.transfers.receive_file(transfer_id, item_id, request.stream())
        except TransferCancelled as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except TransferError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"received": "true"}

    @router.post(
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
        offer: ReceiveOffer = Depends(require_accepted),
    ) -> dict[str, Any] | Response:
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
        except TransferCancelled as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (TransferError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.delete("/api/v1/remote/transfers/{transfer_id}")
    def remote_cancel_transfer(
        transfer_id: str, offer: ReceiveOffer = Depends(require_offer),
    ) -> dict[str, Any]:
        try:
            return context.transfers.cancel_offer(transfer_id)
        except TransferError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/api/v1/remote/transfers/{transfer_id}")
    def remote_transfer_status(
        transfer_id: str, offer: ReceiveOffer = Depends(require_offer),
    ) -> dict[str, Any]:
        if offer.status == "accepted":
            return context.transfers.incoming_status(transfer_id).public_dict()
        return offer.public_dict()

    return router
