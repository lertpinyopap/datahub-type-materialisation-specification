from pathlib import Path
from textwrap import dedent
from typing import Any

from type_materialisation.errors import Diagnostic


def write_spec(directory: Path, name: str, content: str, *, suffix: str = ".yaml") -> Path:
    path = directory / f"{name}{suffix}"
    path.write_text(dedent(content).strip() + "\n", encoding="utf-8")
    return path


def write_csv(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(dedent(content).strip() + "\n", encoding="utf-8")
    return path


def diagnostic_messages(diagnostics: list[Diagnostic]) -> list[str]:
    return [diagnostic.message for diagnostic in diagnostics]


def diagnostic_locations(diagnostics: list[Diagnostic]) -> list[str | None]:
    return [diagnostic.location for diagnostic in diagnostics]


def csv_spec(*, fields: list[dict[str, Any]], source: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "test_spec",
        "source": {
            "format": "csv",
            "header": True,
            **(source or {}),
        },
        "target": {
            "id": "test_target",
            "schema": "business",
            "fields": fields,
        },
    }
