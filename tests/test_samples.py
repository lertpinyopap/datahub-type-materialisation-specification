"""Tests that keep the repository samples honest.

These checks make sure valid YAML and CSV samples continue to parse and validate,
while intentionally broken samples still fail for the expected reasons.
"""

from pathlib import Path

import pytest
import yaml

from type_materialisation.cli import resolve_and_validate_spec
from type_materialisation.csv_validate import validate_csv_file


REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_YAML_DIR = REPO_ROOT / "samples" / "yaml"
SAMPLE_CSV_DIR = REPO_ROOT / "samples" / "csv"


@pytest.mark.parametrize("spec_path", sorted(SAMPLE_YAML_DIR.glob("*.yaml")))
def test_valid_sample_yaml_parses_as_concrete_spec(spec_path: Path) -> None:
    # Samples are documentation examples, so parsing them is part of the contract.
    _, diagnostics = resolve_and_validate_spec(
        spec_path,
        abstract=False,
        variables=_sample_vars(spec_path),
    )

    assert diagnostics == []


def test_valid_account_csv_sample_validates() -> None:
    result = validate_csv_file(
        SAMPLE_YAML_DIR / "account_csv.yaml",
        SAMPLE_CSV_DIR / "account_csv.csv",
    )

    assert result.errors == []
    assert result.rows_checked == 2


def test_valid_account_samples_share_logical_account_id() -> None:
    account_samples = sorted(SAMPLE_YAML_DIR.glob("account*.yaml"))

    for spec_path in account_samples:
        data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        assert data["id"] == "account", spec_path.name
        assert data["target"]["id"] == "account", spec_path.name
        assert data["target"].get("table_name", "account") == "account", spec_path.name
        quarantine = data.get("control_data", {}).get("quarantine")
        if isinstance(quarantine, dict):
            assert quarantine.get("table") == "account__QUARANTINE", spec_path.name


def test_quarantine_sample_csv_has_five_good_rows_and_two_reject_rows() -> None:
    result = validate_csv_file(
        SAMPLE_YAML_DIR / "account_csv_quarantine_sample.yaml",
        SAMPLE_CSV_DIR / "broken" / "account_csv_quarantine_mix.csv",
    )

    assert result.rows_checked == 7
    assert len(result.errors) == 2
    assert [error.location for error in result.errors] == [
        "row 7, field `account_id`",
        "row 8, field `account_id`",
    ]


def test_positional_quarantine_sample_has_two_good_rows_and_one_reject_row() -> None:
    result = validate_csv_file(
        SAMPLE_YAML_DIR / "account_quarantine_sample_positional.yaml",
        SAMPLE_CSV_DIR / "broken" / "account_quarantine_sample_positional.csv",
    )

    assert result.rows_checked == 3
    assert len(result.errors) == 1
    assert result.errors[0].location == "row 3, field `account_id`"


@pytest.mark.parametrize("spec_path", sorted(SAMPLE_YAML_DIR.glob("*.yaml")))
def test_samples_do_not_define_target_database_and_only_reference_tmp_schema(spec_path: Path) -> None:
    data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert _database_paths(data) == []
    assert _schema_values(data) <= {"TMP"}


def _database_paths(value, path: str = "$") -> list[str]:
    if isinstance(value, dict):
        paths = [f"{path}.database" for key in value if key == "database"]
        for key, child in value.items():
            paths.extend(_database_paths(child, f"{path}.{key}"))
        return paths
    if isinstance(value, list):
        paths: list[str] = []
        for index, child in enumerate(value):
            paths.extend(_database_paths(child, f"{path}[{index}]"))
        return paths
    return []


def _schema_values(value) -> set[str]:
    if isinstance(value, dict):
        values = {schema for key, schema in value.items() if key == "schema" and isinstance(schema, str)}
        for child in value.values():
            values.update(_schema_values(child))
        return values
    if isinstance(value, list):
        values: set[str] = set()
        for child in value:
            values.update(_schema_values(child))
        return values
    return set()


def _sample_vars(spec_path: Path) -> dict[str, str]:
    if spec_path.name == "reference_core_country.yaml":
        return {"country_csv_file": str(SAMPLE_CSV_DIR / "REFERENCE.CORE.COUNTRY.csv")}
    return {}


def test_broken_yaml_sample_fails_for_expected_reasons() -> None:
    _, diagnostics = resolve_and_validate_spec(SAMPLE_YAML_DIR / "broken" / "account_csv_broken.yaml", abstract=False)
    messages = [diagnostic.message for diagnostic in diagnostics]

    assert any("does not match" in message for message in messages)
    assert any("duplicates field id" in message for message in messages)
    assert any("unsupported data type" in message for message in messages)
    assert any("invalid regular expression" in message for message in messages)


@pytest.mark.parametrize(
    "csv_path",
    [
        SAMPLE_CSV_DIR / "broken" / "account_csv_bad_account_number.csv",
        SAMPLE_CSV_DIR / "broken" / "account_csv_header_mismatch.csv",
    ],
)
def test_broken_account_csv_samples_fail(csv_path: Path) -> None:
    result = validate_csv_file(SAMPLE_YAML_DIR / "account_csv.yaml", csv_path)

    assert result.errors
