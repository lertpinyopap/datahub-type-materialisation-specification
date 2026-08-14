"""Tests for the Python macro contract used by the implementation.

These checks make sure custom macros can be discovered, must provide SQL
generation, and optionally participate in local CSV validation through Python.
"""

from pathlib import Path

import pytest

from type_materialisation.csv_validate import validate_csv_file
from type_materialisation.custom_macros import MacroLoadError, PythonMacroResolver
from tests.helpers import csv_spec, diagnostic_messages, write_csv, write_spec


def write_macro(directory: Path, module_name: str, content: str) -> Path:
    macro_dir = directory / "macros"
    macro_dir.mkdir(exist_ok=True)
    module_path = macro_dir / f"{module_name}.py"
    module_path.write_text(content.strip() + "\n", encoding="utf-8")
    return macro_dir


def spec_path(tmp_path: Path) -> Path:
    return write_spec(tmp_path, "spec", "id: spec")


def test_missing_macro_module_fails_with_searched_paths(tmp_path: Path) -> None:
    resolver = PythonMacroResolver(spec_path=spec_path(tmp_path), macro_paths=[tmp_path / "macros"])

    with pytest.raises(MacroLoadError, match="was not found; searched"):
        resolver.require_sql_generation("missing_macros.validate_account")


def test_missing_macro_object_fails(tmp_path: Path) -> None:
    macro_dir = write_macro(
        tmp_path,
        "my_macros",
        """
class ExistingMacro:
    def generate_dbt_macro(self):
        return "{% macro existing_macro(column_expression) %}null{% endmacro %}"

existing_macro = ExistingMacro()
""",
    )
    resolver = PythonMacroResolver(spec_path=spec_path(tmp_path), macro_paths=[macro_dir])

    with pytest.raises(MacroLoadError, match="`missing_macro` is not defined"):
        resolver.require_sql_generation("my_macros.missing_macro")


def test_macro_without_sql_generation_fails(tmp_path: Path) -> None:
    macro_dir = write_macro(
        tmp_path,
        "my_macros",
        """
class MissingSqlGeneration:
    pass

missing_sql_generation = MissingSqlGeneration()
""",
    )
    resolver = PythonMacroResolver(spec_path=spec_path(tmp_path), macro_paths=[macro_dir])

    with pytest.raises(MacroLoadError, match="must expose callable `generate_dbt_macro`"):
        resolver.require_sql_generation("my_macros.missing_sql_generation")


def test_sql_only_macro_causes_validate_warning(tmp_path: Path) -> None:
    # Local CSV validation warns when only the dbt SQL side of the macro exists.
    macro_dir = write_macro(
        tmp_path,
        "my_macros",
        """
class SqlOnlyValidation:
    def generate_dbt_macro(self):
        return "{% macro sql_only_validation(column_expression) %}null{% endmacro %}"

sql_only_validation = SqlOnlyValidation()
""",
    )
    spec = csv_spec(
        fields=[
            {
                "id": "account_id",
                "source": {"pos": 0, "column": "account_id"},
                "data_type": "varchar(20)",
                "validations": [{"type": "custom", "macro": "my_macros.sql_only_validation"}],
            }
        ]
    )
    csv_path = write_csv(tmp_path, "input.csv", "account_id\nA001")

    result = validate_csv_file(spec_path(tmp_path), csv_path, macro_paths=[macro_dir], spec=spec)

    assert result.errors == []
    assert len(result.warnings) == 1
    assert "has no Python execution callable" in result.warnings[0].message


def test_python_executable_macro_is_called_by_validate(tmp_path: Path) -> None:
    macro_dir = write_macro(
        tmp_path,
        "my_macros",
        """
class CalledValidation:
    supports_python_execution = True

    def generate_dbt_macro(self):
        return "{% macro called_validation(column_expression) %}null{% endmacro %}"

    def execute(self, *, value):
        return f"called for {value}"

called_validation = CalledValidation()
""",
    )
    spec = csv_spec(
        fields=[
            {
                "id": "account_id",
                "source": {"pos": 0, "column": "account_id"},
                "data_type": "varchar(20)",
                "validations": [{"type": "custom", "macro": "my_macros.called_validation"}],
            }
        ]
    )
    csv_path = write_csv(tmp_path, "input.csv", "account_id\nA001")

    result = validate_csv_file(spec_path(tmp_path), csv_path, macro_paths=[macro_dir], spec=spec)

    assert result.warnings == []
    assert diagnostic_messages(result.errors) == ["called for A001"]
