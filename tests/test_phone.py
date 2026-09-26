from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from relay.app import build_app
from relay.phone import PHONE_COOKIE_NAME, PhoneAccess, PhoneSession


async def stream_bytes(content: bytes) -> Any:
    yield content


def clients(tmp_path: Path) -> tuple[FastAPI, TestClient, TestClient]:
    app = build_app(tmp_path, 9876)
    app.state.context.settings.update(str(tmp_path / "received"))
    app.state.context.addresses = ["192.168.1.10", "127.0.0.1"]
    app.state.context.advertised_address = "192.168.1.10"
    local = TestClient(app, base_url="https://127.0.0.1:9876", client=("127.0.0.1", 50000))
    phone = TestClient(app, base_url="https://192.168.1.10:9876", client=("192.168.1.20", 50000))
    return app, local, phone


def invite(local: TestClient, app: FastAPI) -> str:
    response = local.post(
        "/api/v1/phone/invite",
        headers={"X-Relay-Control-Token": app.state.context.control_token},
    )
    assert response.status_code == 200
    assert response.json()["qr_data_url"].startswith("data:image/png;base64,")
    return str(response.json()["url"]).split("invite=", 1)[1]


def connect(phone: TestClient, token: str) -> None:
    landing = phone.get(f"/phone?invite={token}")
    assert landing.status_code == 200
    assert "Kết nối với Relay" in landing.text
    response = phone.post("/phone/connect", data={"invite": token}, follow_redirects=False)
    assert response.status_code == 303
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "secure" in response.headers["set-cookie"].lower()
    assert phone.cookies.get(PHONE_COOKIE_NAME)


def test_phone_link_requires_local_invite_and_expires_after_use(tmp_path: Path) -> None:
    app, local, phone = clients(tmp_path)
    with local, phone:
        assert phone.get("/").status_code == 403
        assert phone.get("/api/v1/status").status_code == 403
        assert phone.get("/phone").status_code == 401
        assert local.get("/api/v1/state").json()["phone_connection"]["status"] == "disconnected"
        assert local.post("/api/v1/phone/invite").status_code == 403
        token = invite(local, app)
        assert local.get("/api/v1/state").json()["phone_connection"]["status"] == "waiting"
        assert phone.get("/phone?invite=wrong").status_code == 403
        connect(phone, token)
        assert phone.get("/phone").status_code == 200
        assert phone.post("/phone/connect", data={"invite": token}).status_code == 403
        assert phone.get("/phone/api/state").status_code == 200
        assert local.get("/api/v1/state").json()["phone_connection"]["status"] == "connected"
        session = app.state.context.phone.get_session(phone.cookies.get(PHONE_COOKIE_NAME))
        assert session is not None
        session.last_seen_at = time.time() - 30
        assert local.get("/api/v1/state").json()["phone_connection"]["status"] == "disconnected"
        assert phone.get("/phone/api/state").status_code == 200
        assert local.get("/api/v1/state").json()["phone_connection"]["status"] == "connected"

        wrong_host = TestClient(
            app,
            base_url="https://untrusted.example:9876",
            client=("192.168.1.20", 50000),
        )
        with wrong_host:
            assert wrong_host.get("/phone").status_code == 403

        revoked = local.post(
            "/api/v1/phone/revoke",
            headers={"X-Relay-Control-Token": app.state.context.control_token},
        )
        assert revoked.status_code == 200
        assert phone.get("/phone/api/state").status_code == 401
        assert local.get("/api/v1/state").json()["phone_connection"]["status"] == "disconnected"


def test_phone_upload_and_download_are_authorized_and_path_safe(tmp_path: Path) -> None:
    app, local, phone = clients(tmp_path)
    with local, phone:
        connect(phone, invite(local, app))
        session = app.state.context.phone.get_session(phone.cookies.get(PHONE_COOKIE_NAME))
        assert session is not None
        csrf = session.csrf_token
        url = "/phone/api/files?filename=photo.jpg"
        assert phone.post(url, content=b"image").status_code == 403
        headers = {"X-Relay-Phone-Token": csrf}
        assert phone.post(url, content=b"image", headers=headers).status_code == 200
        duplicate = phone.post(url, content=b"second image", headers=headers)
        assert duplicate.status_code == 200
        assert duplicate.json()["name"] == "photo (2).jpg"
        assert phone.post(
            "/phone/api/files?filename=..%2Fescape.txt",
            content=b"no",
            headers=headers,
        ).status_code == 400

        received = app.state.context.settings.load().destination / "From phone"
        assert (received / "photo.jpg").read_bytes() == b"image"
        assert (received / "photo (2).jpg").read_bytes() == b"second image"
        assert not (tmp_path / "escape.txt").exists()
        phone_uploads = local.get("/api/v1/state").json()["phone_uploads"]
        assert [item["name"] for item in phone_uploads] == ["photo (2).jpg", "photo.jpg"]
        assert phone_uploads[0]["path"] == str(received / "photo (2).jpg")
        assert PhoneAccess(tmp_path).recent_uploads() == phone_uploads
        preview_url = f"/api/v1/received/phone/{phone_uploads[0]['id']}"
        preview = local.get(preview_url)
        assert preview.status_code == 200
        assert preview.content == b"second image"
        assert preview.headers["content-disposition"].startswith("inline;")
        assert preview.headers["content-security-policy"].startswith("default-src 'none';")
        assert phone.get(preview_url).status_code == 403
        assert local.get("/api/v1/received/phone/unknown").status_code == 404
        open_path = f"{preview_url}/open"
        assert local.post(open_path).status_code == 403
        with patch("relay.app.open_with_default_app") as launcher:
            opened = local.post(
                open_path,
                headers={"X-Relay-Control-Token": app.state.context.control_token},
            )
            assert opened.status_code == 200
            assert opened.json() == {"opened": True}
            launcher.assert_called_once_with(received / "photo (2).jpg")
        assert phone.post(open_path).status_code == 403

        staged = asyncio.run(
            app.state.context.transfers.stage("report.txt", stream_bytes(b"report"))
        )
        state = phone.get("/phone/api/state")
        assert state.status_code == 200
        assert state.json()["available"][0]["id"] == staged.id
        assert len(state.json()["uploaded"]) == 2
        download = phone.get(f"/phone/api/files/{staged.id}")
        assert download.status_code == 200
        assert download.content == b"report"
        assert download.headers["content-disposition"].startswith("attachment;")
        opened = phone.get(f"/phone/api/files/{staged.id}?preview=1")
        assert opened.status_code == 200
        assert opened.content == b"report"
        assert opened.headers["content-type"].startswith("text/plain")
        assert opened.headers["content-disposition"].startswith("inline;")
        assert phone.get("/phone/api/files/unknown").status_code == 404


def test_phone_sessions_share_one_folder_and_preview_safely(tmp_path: Path) -> None:
    app, local, phone = clients(tmp_path)

    def headers() -> dict[str, str]:
        session = app.state.context.phone.get_session(phone.cookies.get(PHONE_COOKIE_NAME))
        assert session is not None
        return {"X-Relay-Phone-Token": session.csrf_token}

    with local, phone:
        connect(phone, invite(local, app))
        first = phone.post("/phone/api/files?filename=page.html", content=b"<script>1</script>",
                           headers=headers())
        assert first.status_code == 200
        connect(phone, invite(local, app))
        second = phone.post("/phone/api/files?filename=page.html", content=b"different",
                            headers=headers())
        assert second.status_code == 200
        assert second.json()["name"] == "page (2).html"

        root = app.state.context.settings.load().destination
        assert [folder.name for folder in root.iterdir() if folder.is_dir()] == ["From phone"]
        assert (root / "From phone" / "page.html").read_bytes() == b"<script>1</script>"
        assert (root / "From phone" / "page (2).html").read_bytes() == b"different"
        preview = local.get(f"/api/v1/received/phone/{first.json()['id']}")
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("text/plain")
        assert preview.headers["content-disposition"].startswith("inline;")
        assert preview.headers["content-security-policy"].startswith("sandbox;")
        assert local.get(f"/api/v1/received/phone/{first.json()['id']}/preview").status_code == 200
        binary = phone.post(
            "/phone/api/files?filename=archive.bin", content=b"binary", headers=headers()
        )
        assert binary.status_code == 200
        download = local.get(f"/api/v1/received/phone/{binary.json()['id']}")
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["content-disposition"].startswith("attachment;")
        video = phone.post(
            "/phone/api/files?filename=clip.mp4", content=b"video", headers=headers()
        )
        assert video.status_code == 200
        opened_video = local.get(f"/api/v1/received/phone/{video.json()['id']}")
        assert opened_video.headers["content-type"] == "video/mp4"
        assert opened_video.headers["content-disposition"].startswith("inline;")
        assert "sandbox" not in opened_video.headers["content-security-policy"]
        video_preview = local.get(f"/api/v1/received/phone/{video.json()['id']}/preview")
        assert video_preview.status_code == 200
        assert b"<video" in video_preview.content
        assert local.get(f"/api/v1/received/phone/{video.json()['id']}?download=1").headers[
            "content-disposition"
        ].startswith("attachment;")
        Path(first.json()["path"]).unlink()
        unavailable = next(
            item for item in local.get("/api/v1/state").json()["phone_uploads"]
            if item["id"] == first.json()["id"]
        )
        assert unavailable["available"] is False
        assert local.get(f"/api/v1/received/phone/{first.json()['id']}/preview").status_code == 404


def test_parallel_phone_uploads_keep_both_files(tmp_path: Path) -> None:
    access = PhoneAccess(tmp_path)
    destination = tmp_path / "received"

    async def upload_pair() -> list[dict[str, Any]]:
        started = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def stream(index: int) -> Any:
            started[index].set()
            await release.wait()
            yield f"content-{index}".encode()

        tasks = [
            asyncio.create_task(access.receive_file(
                PhoneSession(csrf_token=str(index), expires_at=9999999999),
                destination, "same.txt", stream(index)
            ))
            for index in range(2)
        ]
        await asyncio.gather(*(event.wait() for event in started))
        release.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(upload_pair())
    assert {item["name"] for item in results} == {"same.txt", "same (2).txt"}
    assert {(destination / "From phone" / item["name"]).read_bytes() for item in results} == {
        b"content-0", b"content-1"
    }


def test_phone_invite_expiry_and_interrupted_upload_cleanup(tmp_path: Path) -> None:
    access = PhoneAccess(tmp_path)
    expired_token, _ = access.create_invitation()
    access._invite_expires_at = 0
    assert not access.valid_invitation(expired_token)
    assert access.redeem(expired_token) is None

    token, _ = access.create_invitation()
    redeemed = access.redeem(token)
    assert redeemed is not None
    cookie, session = redeemed

    async def interrupted() -> Any:
        yield b"partial"
        raise OSError("connection interrupted")

    with pytest.raises(OSError, match="connection interrupted"):
        asyncio.run(access.receive_file(session, tmp_path, "video.mp4", interrupted()))
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("video.mp4"))
    assert access.recent_uploads() == []

    access.revoke()
    assert access.get_session(cookie) is None


def test_existing_phone_folder_is_imported_once(tmp_path: Path) -> None:
    destination = tmp_path / "received"
    folder = destination / "From phone (4)"
    folder.mkdir(parents=True)
    (folder / "photo.jpg").write_bytes(b"old photo")
    access = PhoneAccess(tmp_path)
    access.import_existing(destination)
    assert access.recent_uploads()[0]["path"] == str(folder / "photo.jpg")
    assert PhoneAccess(tmp_path).recent_uploads() == access.recent_uploads()
