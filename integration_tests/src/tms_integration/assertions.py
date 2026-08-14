from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            {str(key).upper(): "" if value is None else str(value) for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def normalise_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalised = [
        {str(key).upper(): _normalise_value(value) for key, value in row.items()}
        for row in rows
    ]
    return sorted(normalised, key=lambda row: tuple(row.get(column, "") for column in sorted(row)))


def assert_rows_equal(actual_rows: list[dict[str, Any]], expected_rows: list[dict[str, Any]]) -> None:
    actual = normalise_rows(actual_rows)
    expected = normalise_rows(expected_rows)
    assert actual == expected


def _normalise_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith("+00:00"):
        text = text.removesuffix("+00:00").strip()
    return " ".join(text.split())
