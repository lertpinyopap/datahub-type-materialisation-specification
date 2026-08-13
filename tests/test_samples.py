"""Tests that keep the repository samples honest.

These checks make sure valid YAML and CSV samples continue to parse and validate,
while intentionally broken samples still fail for the expected reasons.
"""

from pathlib import Path

import pytest

from type_materialisation.cli import resolve_and_validate_spec
from type_materialisation.csv_validate import validate_csv_file


REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_YAML_DIR = REPO_ROOT / "samples" / "yaml"
SAMPLE_CSV_DIR = REPO_ROOT / "samples" / "csv"


@pytest.mark.parametrize("spec_path", sorted(SAMPLE_YAML_DIR.glob("*.yaml")))
def test_valid_sample_yaml_parses_as_concrete_spec(spec_path: Path) -> None:
    # Samples are documentation examples, so parsing them is part of the contract.
    _, diagnostics = resolve_and_validate_spec(spec_path, abstract=False)

    assert diagnostics == []


def test_valid_account_csv_sample_validates() -> None:
    result = validate_csv_file(
        SAMPLE_YAML_DIR / "account_csv.yaml",
        SAMPLE_CSV_DIR / "account_csv.csv",
    )

    assert result.errors == []
    assert result.rows_checked == 2


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
