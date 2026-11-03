"""Run with a Python that has Playwright installed; the server uses the project venv."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def serve(data: Path, port: int) -> None:
    import uvicorn

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from test_received_delete import received_files

    from relay.app import build_app

    app = build_app(data / "data", port)
    app.state.context.settings.update(str(data / "received"))
    files = received_files(app, "computer") + received_files(app, "phone")
    (data / "files.json").write_text(
        json.dumps([(url, str(path)) for url, path in files]), encoding="utf-8"
    )
    uvicorn.run(
        app, host="127.0.0.1", port=port, log_level="critical",
        ssl_certfile=str(data / "data" / "identity.crt"),
        ssl_keyfile=str(data / "data" / "identity.key"), loop="asyncio",
    )


def main() -> None:
    import time

    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="relay-delete-ui-") as temporary:
        data = Path(temporary)
        process = subprocess.Popen([
            str(ROOT / ".venv" / "Scripts" / "python.exe"), __file__,
            "--server", str(data), str(port),
        ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(process.communicate())
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.05)
            else:
                raise AssertionError("Test server did not start")
            files = json.loads((data / "files.json").read_text(encoding="utf-8"))
            computer_url, computer_path = files[0]
            phone_url, phone_path = files[2]
            screenshots = ROOT / "scratch" / "received-delete"
            screenshots.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(
                    ignore_https_errors=True, viewport={"width": 1440, "height": 1000}
                )
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("localStorage.setItem('relay-notifications-asked', '1')")
                page.goto(f"https://127.0.0.1:{port}", wait_until="networkidle")
                receive = page.locator("#phoneUploadsList")
                expect(receive.locator("[data-delete-received]")).to_have_count(4)
                page.screenshot(path=str(screenshots / "desktop-before.png"), full_page=True)
                phone_button = receive.locator(f'[data-delete-received="{phone_url}"]')
                page.once("dialog", lambda dialog: dialog.dismiss())
                phone_button.click()
                assert Path(phone_path).exists()
                expect(phone_button).to_be_enabled()
                page.once("dialog", lambda dialog: dialog.accept())
                phone_button.click()
                expect(phone_button).to_have_count(0)
                expect(receive).to_contain_text("Đã xoá tệp")
                assert not Path(phone_path).exists()
                page.locator('[data-view="activity"]').click()
                history = page.locator("#historyList")
                expect(history).to_contain_text("Đã xoá tệp")
                button = history.locator(f'[data-delete-received="{computer_url}"]')
                page.once("dialog", lambda dialog: dialog.accept())
                button.click()
                expect(button).to_have_count(0)
                assert not Path(computer_path).exists()
                assert Path(files[1][1]).exists() and Path(files[3][1]).exists()
                page.reload(wait_until="networkidle")
                expect(page.locator("#phoneUploadsList")).to_contain_text("Đã xoá tệp")
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                expect(page.locator(
                    "#phoneUploadsList [data-delete-received]"
                ).first).to_be_visible()
                page.screenshot(path=str(screenshots / "mobile-after.png"), full_page=True)
                assert not errors, errors
                browser.close()
            print("PASS: cancel/confirm deletion, phone/computer, per-file batch deletion, "
                  "receive/history, reload, mobile layout, no JavaScript errors")
        finally:
            process.terminate()
            process.communicate(timeout=10)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--server":
        serve(Path(sys.argv[2]), int(sys.argv[3]))
    else:
        main()
