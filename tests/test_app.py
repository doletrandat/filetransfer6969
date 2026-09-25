from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from relay.app import build_app


def test_local_interface_reports_device_status(tmp_path: Path) -> None:
    app = build_app(tmp_path, 9876)

    with TestClient(app) as client:
        response = client.get("/api/v1/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["device"]["id"]
    assert payload["device"]["fingerprint"]
    assert Path(payload["destination"]).exists()

    with TestClient(app) as client:
        state = client.get("/api/v1/state")

    assert state.status_code == 200
    assert set(state.json()) == {"status", "devices", "peers", "staged", "outgoing", "incoming"}
