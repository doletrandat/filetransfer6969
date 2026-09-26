from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from relay.app import build_app
from relay.models import DeviceMessage, IncomingManifestRequest, TransferManifestItem
from relay.security import AuthorizedPeer


def local_client(app: FastAPI) -> TestClient:
    return TestClient(
        app, base_url="https://127.0.0.1:9876", client=("127.0.0.1", 50000)
    )


async def stream_bytes(content: bytes) -> AsyncIterator[bytes]:
    yield content


def test_received_computer_file_opens_only_after_completion(tmp_path: Path) -> None:
    app = build_app(tmp_path, 9876)
    app.state.context.settings.update(str(tmp_path / "received"))
    content = b"hello from another computer"
    request = IncomingManifestRequest(
        batch_name="batch",
        source=DeviceMessage(id="sender", name="Sender", fingerprint="A" * 64),
        items=[TransferManifestItem(
            id="file-1", relative_path="hello.txt", size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )],
    )
    transfer = app.state.context.transfers.create_incoming(request)
    path = f"/api/v1/received/computer/{transfer.transfer_id}/file-1"
    with local_client(app) as client:
        assert client.get(path).status_code == 404
        asyncio.run(app.state.context.transfers.receive_file(
            transfer.transfer_id, "file-1", stream_bytes(content)
        ))
        response = client.get(path)
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["content-security-policy"].startswith("sandbox;")
        preview = client.get(f"{path}/preview")
        assert preview.status_code == 200
        assert b"hello.txt" in preview.content
        assert b"<pre class=\"preview-text\">hello from another computer</pre>" in preview.content
        assert f'{path}?download=1'.encode() in preview.content
        assert client.get(f"{path}?download=1").headers["content-disposition"].startswith(
            "attachment;"
        )
        assert client.get(path.replace("file-1", "missing")).status_code == 404
        item = client.get("/api/v1/state").json()["incoming"][0]["items"][0]
        assert item["name"] == "hello.txt"
        assert item["available"] is True
        open_path = f"{path}/open"
        assert client.post(open_path).status_code == 403
        with patch("relay.app.open_with_default_app") as launcher:
            opened = client.post(
                open_path,
                headers={"X-Relay-Control-Token": app.state.context.control_token},
            )
            assert opened.status_code == 200
            assert opened.json() == {"opened": True}
            launcher.assert_called_once_with(tmp_path / "received" / "hello.txt")
            launcher.side_effect = OSError("No associated application")
            failed = client.post(
                open_path,
                headers={"X-Relay-Control-Token": app.state.context.control_token},
            )
            assert failed.status_code == 500
            assert "ứng dụng mặc định" in failed.json()["detail"]
        (tmp_path / "received" / "hello.txt").unlink()
        assert client.get("/api/v1/state").json()["incoming"][0]["items"][0]["available"] is False
        assert client.get(f"{path}/preview").status_code == 404
        assert client.post(
            open_path,
            headers={"X-Relay-Control-Token": app.state.context.control_token},
        ).status_code == 404

    remote = TestClient(
        app, base_url="https://192.168.1.10:9876", client=("192.168.1.20", 50000)
    )
    with remote:
        assert remote.get(path).status_code == 403


def test_local_interface_reports_device_status(tmp_path: Path) -> None:
    app = build_app(tmp_path, 9876)

    with local_client(app) as client:
        response = client.get("/api/v1/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["device"]["id"]
    assert payload["device"]["fingerprint"]
    assert Path(payload["destination"]).exists()

    with local_client(app) as client:
        state = client.get("/api/v1/state")

    assert state.status_code == 200
    assert set(state.json()) == {
        "status", "devices", "peers", "staged", "outgoing", "incoming", "phone_uploads",
        "phone_connection"
    }


def test_local_control_is_private_to_the_device(tmp_path: Path) -> None:
    app = build_app(tmp_path, 9876)
    remote = TestClient(
        app, base_url="https://192.168.1.10:9876", client=("192.168.1.20", 50000)
    )
    with remote:
        assert remote.get("/").status_code == 403
        assert remote.get("/api/v1/status").status_code == 403
        assert remote.post("/api/v1/pairing/code").status_code == 403
        assert remote.post(
            "/api/v1/pairing/code",
            headers={"X-Relay-Control-Token": app.state.context.control_token},
        ).status_code == 403
        assert remote.get("/api/v1/remote/identity").status_code == 200

    with local_client(app) as client:
        assert client.get("/api/v1/status").status_code == 200
        assert client.get("/").headers["Cache-Control"] == "no-store"
        assert client.post("/api/v1/pairing/code").status_code == 403
        assert client.post(
            "/api/v1/pairing/code",
            headers={"X-Relay-Control-Token": app.state.context.control_token},
        ).status_code == 200
        assert app.state.context.control_token in client.get("/").text

    rebound = TestClient(
        app, base_url="https://untrusted.example:9876", client=("127.0.0.1", 50000)
    )
    with rebound:
        assert rebound.get("/api/v1/status").status_code == 403


def test_remote_manifest_must_match_paired_device(tmp_path: Path) -> None:
    app = build_app(tmp_path, 9876)
    peer = AuthorizedPeer(id="sender", name="Sender", fingerprint="A" * 64)
    app.state.context.sessions.register("session-token", peer)
    payload = {
        "batch_name": "batch",
        "source": {"id": "another-device", "name": "Other", "fingerprint": "B" * 64},
        "items": [{
            "id": "file-1", "relative_path": "file.txt", "size": 1, "sha256": "0" * 64
        }],
    }
    remote = TestClient(
        app, base_url="https://192.168.1.10:9876", client=("192.168.1.20", 50000)
    )
    with remote:
        response = remote.post(
            "/api/v1/remote/transfers",
            json=payload,
            headers={"Authorization": "Bearer session-token"},
        )
    assert response.status_code == 403

    payload["source"] = {"id": "sender", "name": "Sender", "fingerprint": "A" * 64}
    with remote:
        created = remote.post(
            "/api/v1/remote/transfers",
            json=payload,
            headers={"Authorization": "Bearer session-token"},
        )
        assert created.status_code == 200
        app.state.context.sessions.register(
            "other-token", AuthorizedPeer(id="other", name="Other", fingerprint="B" * 64)
        )
        assert remote.get(
            f"/api/v1/remote/transfers/{created.json()['transfer_id']}",
            headers={"Authorization": "Bearer other-token"},
        ).status_code == 404
