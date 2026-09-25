from __future__ import annotations

import argparse
import asyncio
import os
import socket
import threading
import webbrowser
from pathlib import Path

import uvicorn

from relay.app import build_app


def default_data_dir() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Relay"
    return Path.home() / ".local" / "share" / "relay"


def available_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise RuntimeError("Relay could not find an available local network port.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transfer files directly between Windows devices.")
    parser.add_argument("--port", type=int, default=8765, help="Preferred local network port")
    parser.add_argument("--host", default="0.0.0.0", help="Address to bind")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--no-browser", action="store_true", help="Do not open the local interface")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    data_dir = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    port = available_port(args.port)
    app = build_app(data_dir, port)
    context = app.state.context
    context.phone.import_existing(context.settings.load().destination)
    context.start()
    if not args.no_browser:
        timer = threading.Timer(
            1.2,
            lambda: webbrowser.open(f"https://127.0.0.1:{port}", new=2),
        )
        timer.daemon = True
        timer.start()
    print(f"Relay is running at https://127.0.0.1:{port}")
    print(f"Receive folder: {context.settings.load().destination}")
    print(f"Security fingerprint: {context.identity.fingerprint}")
    config = uvicorn.Config(
        app,
        host=args.host,
        port=port,
        ssl_certfile=str(data_dir / "identity.crt"),
        ssl_keyfile=str(data_dir / "identity.key"),
        log_level="info",
        access_log=False,
        loop="relay.loop:selector_loop_factory" if os.name == "nt" else "asyncio",
    )
    try:
        uvicorn.Server(config).run()
    finally:
        context.stop()


if __name__ == "__main__":
    main()
