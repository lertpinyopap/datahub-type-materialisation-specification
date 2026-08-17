from __future__ import annotations

import csv
from difflib import unified_diff
from pathlib import Path
from typing import Any


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            {str(key).upper(): "" if value is None else str(value) for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def normalise_rows(rows: list[dict[str, Any]], *, preserve_whitespace: bool = False) -> list[dict[str, str]]:
    normalised = [
        {str(key).upper(): _normalise_value(value, preserve_whitespace=preserve_whitespace) for key, value in row.items()}
        for row in rows
    ]
    return sorted(normalised, key=lambda row: tuple(row.get(column, "") for column in sorted(row)))


def assert_rows_equal(
    actual_rows: list[dict[str, Any]],
    expected_rows: list[dict[str, Any]],
    *,
    preserve_whitespace: bool = False,
) -> None:
    actual = normalise_rows(actual_rows, preserve_whitespace=preserve_whitespace)
    expected = normalise_rows(expected_rows, preserve_whitespace=preserve_whitespace)
    assert actual == expected, _row_diff(actual, expected)


def _normalise_value(value: Any, *, preserve_whitespace: bool) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.endswith("+00:00"):
        text = text.removesuffix("+00:00").strip()
    if preserve_whitespace:
        return text
    text = text.strip()
    return " ".join(text.split())


def _row_diff(actual: list[dict[str, str]], expected: list[dict[str, str]]) -> str:
    actual_lines = [repr(row) for row in actual]
    expected_lines = [repr(row) for row in expected]
    return "\n".join(
        unified_diff(
            expected_lines,
            actual_lines,
            fromfile="expected",
            tofile="actual",
            lineterm="",
        )
    )
