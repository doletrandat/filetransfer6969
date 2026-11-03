from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from relay.app import build_app
from relay.models import DeviceMessage, IncomingManifestRequest, TransferManifestItem
from relay.phone import PhoneSession


async def stream_bytes(content: bytes) -> AsyncIterator[bytes]:
    yield content


def received_files(app: FastAPI, source: str) -> list[tuple[str, Path]]:
    context = app.state.context
    content = b"received file"
    if source == "phone":
        session = PhoneSession(csrf_token="csrf", expires_at=0, device_name="Phone")
        items = [asyncio.run(context.phone.receive_file(
            session, context.settings.load().destination, name, stream_bytes(content)
        )) for name in ("first.txt", "second.txt")]
        return [(f"/api/v1/received/phone/{item['id']}", Path(item["path"])) for item in items]
    manifest = IncomingManifestRequest(
        batch_name="Two files",
        source=DeviceMessage(id="sender", name="Sender", fingerprint="A" * 64),
        items=[TransferManifestItem(
            id=name, relative_path=f"{name}.txt", size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        ) for name in ("first", "second")],
    )
    transfer = context.transfers.create_incoming(manifest)
    for item in manifest.items:
        asyncio.run(context.transfers.receive_file(
            transfer.transfer_id, item.id, stream_bytes(content)
        ))
    return [(f"/api/v1/received/computer/{transfer.transfer_id}/{item.id}", item.target)
            for item in context.transfers.incoming_status(transfer.transfer_id).items]


def local_client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="https://127.0.0.1:9876", client=("127.0.0.1", 50000))


@pytest.mark.parametrize("source", ["computer", "phone"])
def test_delete_received_file_is_local_authorized_and_persistent(
    tmp_path: Path, source: str,
) -> None:
    app = build_app(tmp_path / "data", 9876)
    app.state.context.settings.update(str(tmp_path / "received"))
    (url, path), (_, other) = received_files(app, source)
    headers = {"X-Relay-Control-Token": app.state.context.control_token}
    with local_client(app) as client:
        assert client.delete(url).status_code == 403
        assert path.exists()
        with TestClient(app, base_url="https://192.168.1.10:9876",
                        client=("192.168.1.20", 50000)) as remote:
            assert remote.delete(url, headers=headers).status_code == 403
        assert path.exists()
        assert client.delete(url, headers=headers).json() == {"deleted": True}
        assert not path.exists()
        assert other.read_bytes() == b"received file"
        assert client.get(url).status_code == 404
        assert client.get(f"{url}/preview").status_code == 404
        assert client.post(f"{url}/open", headers=headers).status_code == 404
        assert client.delete(f"{url}-unknown", headers=headers).status_code == 404

    restored = build_app(tmp_path / "data", 9876)
    with local_client(restored) as client:
        state = client.get("/api/v1/state").json()
        if source == "computer":
            assert state["incoming"][0]["status"] == "complete"
            items = state["incoming"][0]["items"]
        else:
            items = state["phone_uploads"]
        deleted = next(item for item in items if item.get("deleted"))
        assert deleted["available"] is False
        # A retry must not delete a new file that has since reused the old path.
        path.write_bytes(b"replacement")
        assert client.delete(url, headers={
            "X-Relay-Control-Token": restored.state.context.control_token
        }).status_code == 200
        assert path.read_bytes() == b"replacement"
        assert client.get(url).status_code == 404


@pytest.mark.parametrize("source", ["computer", "phone"])
def test_delete_received_file_handles_locked_and_missing_files(tmp_path: Path, source: str) -> None:
    app = build_app(tmp_path / "data", 9876)
    app.state.context.settings.update(str(tmp_path / "received"))
    (url, path), _ = received_files(app, source)
    headers = {"X-Relay-Control-Token": app.state.context.control_token}
    with local_client(app) as client:
        with patch.object(Path, "unlink", side_effect=PermissionError("File in use")):
            failed = client.delete(url, headers=headers)
        assert failed.status_code == 409
        assert path.exists()
        assert client.get(url).status_code == 200
        path.unlink()
        assert client.delete(url, headers=headers).status_code == 200
        assert client.get(url).status_code == 404


@pytest.mark.parametrize("source", ["computer", "phone"])
@pytest.mark.parametrize("link_parent", [False, True])
def test_delete_received_file_refuses_symlinks(
    tmp_path: Path, source: str, link_parent: bool,
) -> None:
    app = build_app(tmp_path / "data", 9876)
    app.state.context.settings.update(str(tmp_path / "received"))
    (url, path), _ = received_files(app, source)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / path.name
    victim.write_bytes(b"keep this")
    path.unlink()
    link_guard = nullcontext()
    try:
        if link_parent:
            path.parent.rename(path.parent.with_name("original"))
            path.parent.symlink_to(outside, target_is_directory=True)
        else:
            path.symlink_to(victim)
    except OSError:
        # Windows can require administrator rights for symlinks. Exercise the same
        # readlink result used for both symlinks and directory junctions in that case.
        linked = path.parent if link_parent else path

        def readlink(candidate: Path) -> str:
            if candidate == linked:
                return str(outside)
            raise OSError("Not a link")

        link_guard = patch("relay.transfers.os.readlink", side_effect=readlink)
    with link_guard, local_client(app) as client:
        assert client.delete(url, headers={
            "X-Relay-Control-Token": app.state.context.control_token
        }).status_code == 404
    assert victim.read_bytes() == b"keep this"


def test_delete_received_computer_file_refuses_incomplete_transfer(tmp_path: Path) -> None:
    app = build_app(tmp_path / "data", 9876)
    app.state.context.settings.update(str(tmp_path / "received"))
    (url, path), _ = received_files(app, "computer")
    transfer = app.state.context.transfers.list_incoming()[0]
    transfer.status = "receiving"
    headers = {"X-Relay-Control-Token": app.state.context.control_token}
    with local_client(app) as client:
        assert client.delete(url, headers=headers).status_code == 404
        transfer.status = "complete"
        transfer.items[0].completed = False
        assert client.delete(url, headers=headers).status_code == 404
    assert path.exists()
