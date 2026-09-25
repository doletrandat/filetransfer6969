from __future__ import annotations

from pydantic import BaseModel, Field


class DeviceMessage(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=80)
    fingerprint: str = Field(min_length=47, max_length=95)


class PairRequest(BaseModel):
    code: str = Field(min_length=8, max_length=8)
    device: DeviceMessage


class PairResponse(BaseModel):
    session_token: str
    device: DeviceMessage
    expires_in: int


class StagedItemMessage(BaseModel):
    id: str
    relative_path: str
    size: int = Field(ge=0)
    sha256: str
    staged_at: float


class TransferManifestItem(BaseModel):
    id: str
    relative_path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class StartTransferRequest(BaseModel):
    peer_id: str
    batch_name: str = Field(min_length=1, max_length=120)
    item_ids: list[str] = Field(min_length=1)


class IncomingManifestRequest(BaseModel):
    batch_name: str = Field(min_length=1, max_length=120)
    source: DeviceMessage
    items: list[TransferManifestItem] = Field(min_length=1)


class IncomingManifestResponse(BaseModel):
    transfer_id: str
    completed_item_ids: list[str]
    destination: str


class DestinationRequest(BaseModel):
    destination: str = Field(min_length=1, max_length=260)
