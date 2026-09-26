from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_default_receive_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never write received files to the user's Downloads/Relay folder."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
