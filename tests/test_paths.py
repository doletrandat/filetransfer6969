from __future__ import annotations

from pathlib import Path

import pytest

from relay.transfers import (
    UnsafePathError,
    device_folder_name,
    safe_join,
    sanitize_relative_path,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("report.txt", "report.txt"),
        ("Project/docs/report.txt", "Project/docs/report.txt"),
        ("Project\\docs\\report.txt", "Project/docs/report.txt"),
        ("bad:name?.txt", "bad_name_.txt"),
    ],
)
def test_sanitize_relative_path(value: str, expected: str) -> None:
    assert sanitize_relative_path(value) == Path(expected)


@pytest.mark.parametrize(
    "value", ["../secret.txt", "C:/secret.txt", "/absolute.txt", "..\\secret.txt"]
)
def test_sanitize_relative_path_rejects_escapes(value: str) -> None:
    with pytest.raises(UnsafePathError):
        sanitize_relative_path(value)


def test_safe_join_stays_inside_root(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "folder/file.txt") == (tmp_path / "folder" / "file.txt").resolve()


@pytest.mark.parametrize("name", ["../Other/PC", "C:\\escape", "CON.txt", "...", "A" * 200])
def test_device_names_are_single_safe_folders(tmp_path: Path, name: str) -> None:
    folder = device_folder_name(name)
    target = safe_join(tmp_path, folder)
    assert target.parent == tmp_path.resolve()
    assert len(folder) <= 81
    assert folder.split(".")[0].upper() != "CON"
