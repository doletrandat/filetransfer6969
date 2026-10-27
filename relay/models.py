# Các mô hình Pydantic kiểm tra dữ liệu thiết bị, manifest và yêu cầu truyền.
from __future__ import annotations

from pydantic import BaseModel, Field


# Mô tả thiết bị xuất hiện trong thông tin trao đổi giữa hai máy.
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


# Mỗi mục manifest mô tả file bằng ID, đường dẫn, kích thước và SHA-256.
class TransferManifestItem(BaseModel):
    id: str
    relative_path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0, le=1024**4)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class StartTransferRequest(BaseModel):
    peer_id: str
    batch_name: str = Field(min_length=1, max_length=120)
    item_ids: list[str] = Field(min_length=1)


# Giới hạn số file và cấu trúc manifest trước khi tạo yêu cầu nhận.
class IncomingManifestRequest(BaseModel):
    batch_name: str = Field(min_length=1, max_length=120)
    source: DeviceMessage
    items: list[TransferManifestItem] = Field(min_length=1, max_length=10000)


class IncomingManifestResponse(BaseModel):
    transfer_id: str
    completed_item_ids: list[str]
    destination: str


class DestinationRequest(BaseModel):
    destination: str = Field(min_length=1, max_length=260)
