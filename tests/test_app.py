from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from relay.app import build_app
from relay.security import AuthorizedPeer


def local_client(app: FastAPI) -> TestClient:
    return TestClient(
        app, base_url="https://127.0.0.1:9876", client=("127.0.0.1", 50000)
    )


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
    assert set(state.json()) == {"status", "devices", "peers", "staged", "outgoing", "incoming"}


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
