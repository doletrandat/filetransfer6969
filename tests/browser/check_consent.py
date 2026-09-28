"""Verify three-file consent, rejection, cancellation and progress with two real HTTPS apps."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import uvicorn
from check_send_progress import ROOT, open_port, run_server
from playwright.sync_api import FilePayload, expect, sync_playwright

from relay.app import AppContext, create_app
from relay.discovery import DiscoveredDevice


def main() -> None:
    servers = []
    screenshots = ROOT / "scratch" / "consent"
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="relay-consent-") as temporary:
        try:
            contexts = []
            for name in ("Máy A", "Máy B"):
                context = AppContext(Path(temporary) / name, open_port())
                context.identity = replace(context.identity, name=name)
                context.transfers._identity = context.identity
                context.settings.update(str(Path(temporary) / name / "received"))
                server = uvicorn.Server(uvicorn.Config(
                    create_app(context), host="127.0.0.1", port=context.port,
                    ssl_certfile=str(context.data_dir / "identity.crt"),
                    ssl_keyfile=str(context.data_dir / "identity.key"), log_level="critical",
                ))
                thread = threading.Thread(target=run_server, args=(server,), daemon=True)
                thread.start()
                servers.append((server, thread))
                deadline = time.monotonic() + 10
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert server.started
                contexts.append(context)
            sender, receiver = contexts
            sender.discovery._devices[receiver.identity.id] = DiscoveredDevice(
                receiver.identity.id, receiver.identity.name, "127.0.0.1", receiver.port,
                receiver.identity.fingerprint, "", time.time(),
            )
            original = receiver.transfers.receive_chunk

            async def slow_receive(*args: Any, **kwargs: Any) -> Any:
                await asyncio.sleep(0.2)
                return await original(*args, **kwargs)

            receiver.transfers.receive_chunk = slow_receive  # type: ignore[method-assign]
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                browser_context = browser.new_context(ignore_https_errors=True,
                                                      viewport={"width": 1440, "height": 1000})
                browser_context.add_init_script(
                    "localStorage.setItem('relay-notifications-asked', '1')"
                )
                a, b = browser_context.new_page(), browser_context.new_page()
                errors = []
                for page, app in ((a, sender), (b, receiver)):
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"https://127.0.0.1:{app.port}", wait_until="networkidle")
                a.locator('[data-view="send"]').click()
                b.locator('[data-view="activity"]').click()
                assert a.locator("#pairingCode").count() == 0
                a.locator('#deviceList input[type="checkbox"]').check()
                files = [FilePayload(name=f"file-{i}.bin", mimeType="application/octet-stream",
                                     buffer=bytes([i]) * (size * 1024 * 1024))
                         for i, size in enumerate((12, 8, 5))]
                a.locator("#fileInput").set_input_files(files)
                expect(a.locator("#sendButton")).to_be_enabled(timeout=15000)
                a.locator("#sendButton").click()
                expect(b.locator("#requestDialog")).to_be_visible(timeout=10000)
                expect(b.locator("#requestList h3")).to_have_text(
                    "Máy A muốn gửi 3 tệp, tổng 25.0 MB"
                )
                expect(a.locator(".send-transfer .transfer-state").first).to_have_text(
                    "Chờ máy nhận đồng ý"
                )
                assert receiver.transfers.list_incoming() == []
                assert not list(receiver.settings.load().destination.rglob("*"))
                b.screenshot(path=str(screenshots / "request-desktop.png"))
                b.set_viewport_size({"width": 390, "height": 844})
                assert b.evaluate("document.documentElement.scrollWidth <= innerWidth")
                b.screenshot(path=str(screenshots / "request-mobile.png"))
                b.locator('[data-decision="reject"]').click()
                expect(b.locator("#requestDialog")).to_be_hidden()
                expect(a.locator(".send-transfer .transfer-state").first).to_have_text("Đã từ chối")
                assert receiver.transfers.list_incoming() == []
                a.locator("#sendButton").click()
                expect(b.locator("#requestDialog")).to_be_visible()
                a.locator('.send-transfer').first.get_by_role("button", name="Hủy gửi").click()
                expect(b.locator("#requestDialog")).to_be_hidden()
                expect(a.locator(".send-transfer .transfer-state").first).to_have_text("Đã hủy")
                a.locator("#sendButton").click()
                expect(b.locator("#requestDialog")).to_be_visible()
                b.locator('[data-decision="accept"]').click()
                expect(b.locator("#requestDialog")).to_be_hidden()
                expect(b.locator("#receiveView")).to_be_visible()
                expect(a.locator(".send-transfer .transfer-state").first).to_have_text("Đang gửi")
                expect(b.locator("#phoneUploadsList")).to_contain_text("Đang nhận")
                expect(a.locator(".send-transfer .transfer-state").first).to_have_text(
                    "Hoàn tất", timeout=30000,
                )
                expect(b.locator("#phoneUploadsList")).to_contain_text("Hoàn tất")
                transfer = receiver.transfers.list_incoming()[0]
                assert transfer.received_bytes == 25 * 1024 * 1024
                for item, payload in zip(transfer.items, files, strict=True):
                    assert item.target.read_bytes() == payload["buffer"]
                assert not errors, errors
                browser.close()
                print("PASS: 3 files / 25 MB, cross-tab prompt, reject, cancel waiting, "
                      "fresh consent, progress and verified contents, mobile, no JS errors")
        finally:
            for server, thread in servers:
                server.should_exit = True
                thread.join(timeout=10)


if __name__ == "__main__":
    main()
