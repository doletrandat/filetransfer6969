# Tạo event loop selector tương thích với luồng chạy Uvicorn trên Windows.
from __future__ import annotations

import asyncio


def selector_loop_factory(use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    del use_subprocess
    return asyncio.SelectorEventLoop()
