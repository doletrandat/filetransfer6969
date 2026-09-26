from __future__ import annotations

from pathlib import Path

import pytest

from relay.transfers import (
    UnsafePathError,
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
