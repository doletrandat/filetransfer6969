"""Live browser regression for multi-device sending and first-visit notifications."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import uvicorn
from check_send_progress import ROOT, open_port, run_server
from playwright.sync_api import expect, sync_playwright

from relay.app import AppContext, create_app
from relay.discovery import DiscoveredDevice
from relay.transfers import device_folder_name


def main() -> None:
    servers = []
    screenshots = ROOT / "scratch" / "multi-device"
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="relay-multi-") as temporary:
        try:
            contexts = []
            for name in ("Sender", "Laptop Alpha", "Desktop Beta"):
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
            sender, first, second = contexts
            for receiver in (first, second):
                sender.discovery._devices[receiver.identity.id] = DiscoveredDevice(
                    receiver.identity.id, receiver.identity.name, "127.0.0.1", receiver.port,
                    receiver.identity.fingerprint, "", time.time(),
                )
                receiver.discovery._devices[sender.identity.id] = DiscoveredDevice(
                    sender.identity.id, sender.identity.name, "127.0.0.1", sender.port,
                    sender.identity.fingerprint, "", time.time(),
                )
                original = receiver.transfers.receive_chunk

                async def slow_receive(*args: Any, _receive: Any = original, **kwargs: Any) -> Any:
                    await asyncio.sleep(0.5)
                    return await _receive(*args, **kwargs)

                receiver.transfers.receive_chunk = slow_receive  # type: ignore[method-assign]
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1000}, ignore_https_errors=True,
                )
                receiver_pages = {}
                for receiver in (first, second):
                    receiver_page = browser.new_page(ignore_https_errors=True)
                    receiver_page.add_init_script(
                        "localStorage.setItem('relay-notifications-asked', '1')"
                    )
                    receiver_page.goto(f"https://127.0.0.1:{receiver.port}")
                    receiver_pages[receiver.identity.id] = receiver_page

                def accept_request(receiver: AppContext) -> None:
                    receiver_page = receiver_pages[receiver.identity.id]
                    expect(receiver_page.locator("#requestDialog")).to_be_visible(timeout=10000)
                    receiver_page.locator('[data-decision="accept"]').click()
                    expect(receiver_page.locator("#requestDialog")).to_be_hidden()

                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                # A deterministic permission stub verifies the user-gesture request and persistence.
                page.add_init_script("""window.permissionCalls = 0;
                    window.Notification = class {
                        static permission = 'default';
                        static requestPermission() {
                            window.permissionCalls++;
                            return Promise.resolve('granted');
                        }
                    };""")
                page.goto(f"https://127.0.0.1:{sender.port}", wait_until="networkidle")
                expect(page.locator("#notificationDialog")).to_be_visible()
                page.locator("#enableNotificationsButton").click()
                assert page.evaluate("window.permissionCalls") == 1
                expect(page.locator("#notificationDialog")).to_be_hidden()
                page.reload(wait_until="networkidle")
                expect(page.locator("#notificationDialog")).to_be_hidden()
                assert page.evaluate("window.permissionCalls") == 0
                expect(page.locator("#connectedDevicesList")).to_contain_text("Laptop Alpha")
                expect(page.locator("#connectedDevicesList")).to_contain_text("Desktop Beta")
                page.locator('[data-view="send"]').click()
                checkboxes = page.locator('#deviceList input[type="checkbox"]')
                expect(checkboxes).to_have_count(2)
                # Explicitly clearing selection must survive state polling.
                for checkbox in checkboxes.all():
                    checkbox.uncheck()
                page.wait_for_timeout(2200)
                expect(page.locator('#deviceList input:checked')).to_have_count(0)
                for checkbox in checkboxes.all():
                    checkbox.check()
                content = b"multi device payload" * 250000
                page.locator("#fileInput").set_input_files({
                    "name": "shared.bin", "mimeType": "application/octet-stream", "buffer": content,
                })
                expect(page.locator("#sendButton")).to_have_text("Gửi đến 2 thiết bị")
                page.locator("#sendButton").click()
                expect(page.locator("#sidebarProgressList .sidebar-transfer")).to_have_count(2)
                accept_request(first)
                accept_request(second)
                page.screenshot(path=str(screenshots / "desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(screenshots / "mobile.png"), full_page=True)
                expect(page.locator(".send-transfer .transfer-state.complete")).to_have_count(
                    2, timeout=30000,
                )
                for receiver in (first, second):
                    path = receiver.settings.load().destination / device_folder_name(
                        sender.identity.name
                    ) / "shared.bin"
                    assert path.read_bytes() == content
                assert len(sender.transfers.list_outgoing()) == 2
                expect(page.locator("#sidebarProgressList")).to_contain_text("Không có lượt truyền")
                expect(page.locator("#clearStagedButton")).to_be_enabled()
                page.locator("#clearStagedButton").click()
                # A refused target must not prevent the other target from receiving.
                def reject_second(route: Any) -> None:
                    if route.request.post_data_json["peer_id"] == second.identity.id:
                        route.fulfill(status=400, content_type="application/json", body=json.dumps({
                            "detail": "Receiver unavailable",
                        }))
                    else:
                        route.continue_()

                page.route("**/api/v1/transfers", reject_second)
                page.locator("#fileInput").set_input_files({
                    "name": "partial.txt", "mimeType": "text/plain", "buffer": b"partial success",
                })
                expect(page.locator("#sendButton")).to_be_enabled()
                page.locator("#sendButton").click()
                expect(page.locator("#toast")).to_contain_text("Desktop Beta: Receiver unavailable")
                expect(page.locator('#deviceList input:checked')).to_have_count(1)
                accept_request(first)
                expect(page.locator(".send-transfer .transfer-state.complete")).to_have_count(
                    3, timeout=15000,
                )
                page.unroute("**/api/v1/transfers")
                page.locator("#sendButton").click()
                accept_request(second)
                expect(page.locator(".send-transfer .transfer-state.complete")).to_have_count(
                    4, timeout=15000,
                )
                assert len(sender.transfers.list_outgoing()) == 4
                receiver_page = browser.new_page(ignore_https_errors=True)
                receiver_page.goto(f"https://127.0.0.1:{first.port}", wait_until="networkidle")
                expect(receiver_page.locator("#connectedDevicesList")).to_contain_text("Sender")
                # Simulate the browser's model API, not a user-supplied display name.
                sender.addresses.append("localhost")
                invite, _ = sender.phone.create_invitation()
                phone_page = browser.new_page(
                    viewport={"width": 390, "height": 844}, ignore_https_errors=True,
                )
                phone_page.add_init_script("""Object.defineProperty(navigator, 'userAgentData', {
                    value: {getHighEntropyValues: async () => ({model: 'Pixel 9'})}
                });""")
                phone_page.goto(
                    f"https://localhost:{sender.port}/phone?invite={invite}",
                    wait_until="networkidle",
                )
                expect(phone_page.locator('input:not([type="hidden"])')).to_have_count(0)
                phone_page.screenshot(path=str(screenshots / "phone-connect.png"), full_page=True)
                phone_page.get_by_role("button", name="Kết nối với Relay").click()
                expect(page.locator("#connectedDevicesList")).to_contain_text("Pixel 9")
                expect(page.locator("#phoneHeaderStatus")).to_have_count(0)
                expect(page.locator(".connection-pills")).not_to_contain_text("Điện thoại kết nối")
                # Unbroken long names must fit narrow screens without moving controls off-screen.
                def long_names(route: Any) -> None:
                    response = route.fetch()
                    payload = response.json()
                    for peer in payload["devices"]:
                        peer["name"] = "Workstation" * 7
                    route.fulfill(response=response, json=payload)

                page.route("**/api/v1/state", long_names)
                expect(page.locator("#connectedDevicesList")).to_contain_text("Workstation" * 7)
                page.screenshot(path=str(screenshots / "long-names.png"), full_page=True)
                overflowing = page.evaluate(
                    """[...document.querySelectorAll('body *')].filter(e =>
                        e.getBoundingClientRect().right > innerWidth + 1).map(e =>
                        ({tag:e.tagName, id:e.id, cls:e.className,
                          width:e.getBoundingClientRect().width}))"""
                )
                fits = page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert fits, overflowing
                assert not errors, errors
                browser.close()
                print(
                    "PASS: first-visit prompt, selection, two real receivers, sidebar, mobile, "
                    "partial failure and retry"
                )
        finally:
            for server, thread in servers:
                server.should_exit = True
                thread.join(timeout=10)


if __name__ == "__main__":
    main()
