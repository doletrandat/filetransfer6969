"""Optional live UI check: python tests/browser/check_send_progress.py.

Requires Playwright and Edge. Uses temporary data and two loopback HTTPS services;
no traffic is sent to discovered LAN devices.
"""
from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from relay import transfers as transfer_module  # noqa: E402
from relay.app import AppContext, create_app  # noqa: E402
from relay.transfers import TransferError  # noqa: E402


def open_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run_server(server: uvicorn.Server) -> None:
    # Match Relay's socket loop without changing Playwright's subprocess loop.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(server.serve())


def main() -> None:
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []
    screenshots = ROOT / "scratch" / "send-progress"
    screenshots.mkdir(parents=True, exist_ok=True)
    original_chunk_size = transfer_module.CHUNK_SIZE
    transfer_module.CHUNK_SIZE = 256 * 1024
    try:
        with tempfile.TemporaryDirectory(prefix="relay-progress-") as temporary:
            contexts = []
            for name in ("sender", "receiver"):
                context = AppContext(Path(temporary) / name, open_port())
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
            ticket = receiver.pairing.create_ticket()
            sender.transfers.pair(
                ticket.code, endpoint=f"https://127.0.0.1:{receiver.port}",
                fingerprint=receiver.identity.fingerprint,
            )
            receive_chunk = receiver.transfers.receive_chunk
            reject_chunks = False

            async def slow_receive(*args: Any, **kwargs: Any) -> dict[str, Any]:
                await asyncio.sleep(0.3)
                if reject_chunks:
                    raise TransferError("Test receiver temporarily unavailable")
                return await receive_chunk(*args, **kwargs)

            receiver.transfers.receive_chunk = slow_receive  # type: ignore[method-assign]
            start_outgoing = sender.transfers.start_outgoing

            def slow_start(*args: Any, **kwargs: Any) -> Any:
                time.sleep(1)
                return start_outgoing(*args, **kwargs)

            sender.transfers.start_outgoing = slow_start  # type: ignore[method-assign]
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1000}, ignore_https_errors=True,
                )
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"https://127.0.0.1:{sender.port}", wait_until="networkidle")
                page.locator('[data-view="send"]').click()
                expect(page.locator("#sendProgress")).to_be_hidden()
                page.locator("#fileInput").set_input_files([
                    {"name": "first.bin", "mimeType": "application/octet-stream",
                     "buffer": b"a" * (4 * 1024 * 1024)},
                    {"name": "second.bin", "mimeType": "application/octet-stream",
                     "buffer": b"b" * (4 * 1024 * 1024)},
                ])
                expect(page.locator("#sendButton")).to_be_enabled(timeout=10000)
                page.locator("#sendButton").click()
                expect(page.locator("#sendPending")).to_be_visible()
                expect(page.locator("#sendButton")).to_be_disabled()
                # Repeated clicks cannot start a duplicate transfer while preparing.
                page.locator("#sendButton").dispatch_event("click")
                card = page.locator(".send-transfer").first
                expect(card).to_be_visible(timeout=10000)
                page.wait_for_function("""() => {
                    const bar = document.querySelector('#sendProgressList [role="progressbar"]');
                    return bar && +bar.getAttribute('aria-valuenow') > 0;
                }""")
                assert len(sender.transfers.list_outgoing()) == 1
                expect(card.locator('[data-send-field="speed"]')).to_contain_text("/s")
                expect(card.locator('[data-send-field="eta"]')).to_contain_text("Khoảng")
                expect(card).to_contain_text("Tệp hiện tại: first.bin")
                expect(page.locator("#clearStagedButton")).to_be_disabled()
                expect(page.locator(".file-card-remove").first).to_be_disabled()
                page.screenshot(path=str(screenshots / "desktop-sending.png"), full_page=True)

                # A lost local API connection must not present stale speed as live.
                page.route("**/api/v1/state", lambda route: route.abort())
                expect(page.locator("#sendConnectionWarning")).to_be_visible(timeout=10000)
                expect(card.locator('[data-send-field="speed"]')).to_have_text("Đang chờ dữ liệu…")
                page.unroute("**/api/v1/state")
                expect(page.locator("#sendConnectionWarning")).to_be_hidden(timeout=10000)

                page.set_viewport_size({"width": 390, "height": 844})
                page.locator("#sendProgress").scroll_into_view_if_needed()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                ), "Mobile layout overflows horizontally"
                page.screenshot(path=str(screenshots / "mobile-sending.png"), full_page=True)
                expect(card).to_contain_text("Tệp hiện tại: second.bin", timeout=15000)
                expect(card).to_contain_text("1/2 tệp hoàn tất")
                expect(card.locator(".transfer-state")).to_have_text("Hoàn tất", timeout=30000)
                expect(card.locator('[role="progressbar"]')).to_have_attribute(
                    "aria-valuenow", "100",
                )
                expect(card).to_contain_text("2/2 tệp hoàn tất")
                expect(card.locator('[data-send-field="bytes"]')).to_have_text("8.0 MB / 8.0 MB")
                received_dir = Path(temporary) / "receiver/received"
                assert (received_dir / "first.bin").read_bytes() == b"a" * (4 * 1024 * 1024)
                assert (received_dir / "second.bin").read_bytes() == b"b" * (4 * 1024 * 1024)
                page.reload(wait_until="networkidle")
                page.locator('[data-view="send"]').click()
                expect(card.locator(".transfer-state")).to_have_text("Hoàn tất")

                reject_chunks = True
                page.locator("#fileInput").set_input_files({
                    "name": "retry.txt", "mimeType": "text/plain", "buffer": b"retry me",
                })
                expect(page.locator("#sendButton")).to_be_enabled()
                page.locator("#sendButton").click()
                expect(card.locator(".transfer-state")).to_have_text("Gửi thất bại", timeout=20000)
                expect(card).to_contain_text("Test receiver temporarily unavailable")
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.screenshot(path=str(screenshots / "desktop-failed.png"), full_page=True)
                reject_chunks = False
                card.get_by_role("button", name="Thử gửi lại").click()
                expect(card.locator(".transfer-state")).to_have_text("Hoàn tất", timeout=15000)
                assert len(sender.transfers.list_outgoing()) == 2
                assert (Path(temporary) / "receiver/received/retry.txt").read_bytes() == b"retry me"

                page.locator("#fileInput").set_input_files({
                    "name": "empty.txt", "mimeType": "text/plain", "buffer": b"",
                })
                expect(page.locator("#sendButton")).to_be_enabled()
                page.locator("#sendButton").click()
                expect(card.locator(".send-transfer-name")).to_have_text("empty.txt")
                expect(card.locator(".transfer-state")).to_have_text("Hoàn tất", timeout=10000)
                expect(card.locator('[role="progressbar"]')).to_have_attribute(
                    "aria-valuenow", "100",
                )
                page.screenshot(path=str(screenshots / "desktop-complete.png"), full_page=True)
                page.locator("#viewSendHistoryButton").click()
                expect(page.locator("#activityView")).to_be_visible()
                assert not errors, errors
                browser.close()
            for server, thread in servers:
                server.should_exit = True
                thread.join(timeout=10)
            print(
                "PASS: real HTTPS send, progress, duplicate guard, reconnect, mobile, "
                "completion, reload, failure, retry, empty file; no JS errors"
            )
    finally:
        transfer_module.CHUNK_SIZE = original_chunk_size
        for server, thread in servers:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
