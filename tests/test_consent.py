from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relay.app import build_app


def manifest() -> dict[str, Any]:
    return {
        "batch_name": "batch",
        "source": {"id": "sender", "name": "Sender", "fingerprint": "A" * 64},
        "items": [{"id": "one", "relative_path": "file.txt", "size": 4,
                   "sha256": hashlib.sha256(b"data").hexdigest()}],
    }


def test_consent_is_local_and_token_is_scoped_to_immutable_batch(tmp_path: Path) -> None:
    app = build_app(tmp_path / "receiver", 9876)
    app.state.context.settings.update(str(tmp_path / "received"))
    with TestClient(app, base_url="https://127.0.0.1:9876",
                    client=("127.0.0.1", 50000)) as local, TestClient(
        app, base_url="https://192.168.1.10:9876", client=("192.168.1.20", 50000),
    ) as remote:
        first = remote.post("/api/v1/remote/transfers", json=manifest()).json()
        second = remote.post("/api/v1/remote/transfers", json=manifest()).json()
        headers = {"Authorization": f"Bearer {first['token']}"}
        control = {"X-Relay-Control-Token": app.state.context.control_token}
        url = f"/api/v1/remote/transfers/{first['id']}"
        assert remote.get(url).status_code == 403
        assert remote.get(f"/api/v1/remote/transfers/{second['id']}",
                          headers=headers).status_code == 403
        for suffix in ("", "/chunks?offset=0&total_size=4&final=true"):
            assert remote.post(url + "/files/one" + suffix, headers=headers,
                               content=b"data").status_code == 403
        assert not list((tmp_path / "received").rglob("*"))
        accept = f"/api/v1/requests/{first['id']}/accept"
        assert remote.post(accept, headers=control).status_code == 403
        assert local.post(accept).status_code == 403
        assert local.post(accept, headers=control).status_code == 200
        assert local.post(accept, headers=control).status_code == 409
        assert "token" not in local.get("/api/v1/state").text
        assert remote.post(url + "/files/not-approved", headers=headers,
                           content=b"data").status_code == 400
        uploaded = remote.post(url + "/files/one/chunks?offset=0&total_size=4&final=true",
                               headers=headers, content=b"data")
        assert uploaded.status_code == 200
        assert remote.get(url, headers=headers).json()["status"] == "complete"
        second_headers = {"Authorization": f"Bearer {second['token']}"}
        assert remote.post(f"/api/v1/remote/transfers/{second['id']}/files/one",
                           headers=second_headers, content=b"data").status_code == 403
        assert local.post(f"/api/v1/requests/{second['id']}/reject",
                          headers=control).status_code == 200
        assert local.post(f"/api/v1/requests/{second['id']}/accept",
                          headers=control).status_code == 409
        assert remote.post("/api/v1/remote/pair", json={}).status_code == 404
        assert (tmp_path / "received" / "Sender" / "file.txt").read_bytes() == b"data"


def test_expired_and_cancelled_requests_cannot_be_accepted(tmp_path: Path) -> None:
    app = build_app(tmp_path, 9876)
    with TestClient(app, base_url="https://127.0.0.1:9876",
                    client=("127.0.0.1", 50000)) as client:
        control = {"X-Relay-Control-Token": app.state.context.control_token}
        for expired in (True, False):
            offer = client.post("/api/v1/remote/transfers", json=manifest()).json()
            headers = {"Authorization": f"Bearer {offer['token']}"}
            url = f"/api/v1/remote/transfers/{offer['id']}"
            if expired:
                app.state.context.transfers._offers[offer["id"]].expires_at = 0
            else:
                assert client.delete(url, headers=headers).status_code == 200
            assert client.get(url, headers=headers).json()["status"] == (
                "expired" if expired else "cancelled"
            )
            assert client.post(f"/api/v1/requests/{offer['id']}/accept",
                               headers=control).status_code == 409
            assert client.post(url + "/files/one", headers=headers,
                               content=b"data").status_code == 403


def test_invalid_manifest_and_pending_limit(tmp_path: Path) -> None:
    app = build_app(tmp_path, 9876)
    with TestClient(app, base_url="https://127.0.0.1:9876") as client:
        payload = manifest()
        payload["items"][0]["relative_path"] = "../escape.txt"
        assert client.post("/api/v1/remote/transfers", json=payload).status_code == 400
        for _ in range(32):
            assert client.post("/api/v1/remote/transfers", json=manifest()).status_code == 200
        assert client.post("/api/v1/remote/transfers", json=manifest()).status_code == 400
