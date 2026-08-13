"""Tests for validating CSV files against a resolved type materialisation spec.

These checks cover CSV column selection, dialect settings, data type parsing,
nullability, uniqueness, and row-level validation errors.
"""

from pathlib import Path

import pytest

from type_materialisation.csv_validate import validate_csv_file
from tests.helpers import csv_spec, diagnostic_messages, write_csv, write_spec


def field(field_id: str, data_type: str, *, pos: int = 0, column: str | None = None, **extra):
    return {
        "id": field_id,
        "source": {
            "pos": pos,
            "column": column or field_id,
        },
        "data_type": data_type,
        **extra,
    }


def validate(tmp_path: Path, spec: dict, csv_content: str):
    spec_path = write_spec(tmp_path, "spec", "id: spec")
    csv_path = write_csv(tmp_path, "input.csv", csv_content)
    return validate_csv_file(spec_path, csv_path, spec=spec)


def test_position_and_column_header_mismatch_is_an_error(tmp_path: Path) -> None:
    # Supplying both selectors is allowed only when they identify the same CSV column.
    spec = csv_spec(fields=[field("account_id", "varchar(20)", pos=0, column="account_id")])

    result = validate(tmp_path, spec, "wrong_id\nACCT000000000001")

    assert result.rows_checked == 1
    assert any("expected `account_id`" in message for message in diagnostic_messages(result.errors))


def test_nullable_false_rejects_empty_values(tmp_path: Path) -> None:
    spec = csv_spec(fields=[field("account_id", "varchar(20)", nullable=False)])

    result = validate(tmp_path, spec, 'account_id\n""')

    assert result.rows_checked == 1
    assert diagnostic_messages(result.errors) == ["is null but field is not nullable"]
    assert result.errors[0].location == "row 2, field `account_id`"


def test_unique_rejects_duplicate_values(tmp_path: Path) -> None:
    spec = csv_spec(fields=[field("account_id", "varchar(20)", unique=True)])

    result = validate(tmp_path, spec, "account_id\nA001\nA001")

    assert result.rows_checked == 2
    assert diagnostic_messages(result.errors) == ["duplicates a value for a unique field"]
    assert result.errors[0].location == "row 3, field `account_id`"


def test_python_csv_dialect_names_are_used(tmp_path: Path) -> None:
    # The spec uses Python's CSV dialect field names directly.
    spec = csv_spec(
        source={
            "delimiter": "|",
            "quotechar": "'",
            "lineterminator": "\n",
            "quoting": "minimal",
        },
        fields=[
            field("account_id", "varchar(20)", pos=0),
            field("account_name", "varchar(255)", pos=1),
        ],
    )

    result = validate(tmp_path, spec, "account_id|account_name\nA001|'Bank | Name'")

    assert result.errors == []
    assert result.rows_checked == 1


def test_csv_validator_rejects_unsupported_quoting_for_direct_specs(tmp_path: Path) -> None:
    spec = csv_spec(source={"quoting": "none"}, fields=[field("account_id", "varchar(20)")])

    result = validate(tmp_path, spec, "account_id\nA001")

    assert diagnostic_messages(result.errors) == ["quoting must be one of: minimal, all"]
    assert result.errors[0].location == "$.source.quoting"


def test_varchar_length_is_enforced(tmp_path: Path) -> None:
    spec = csv_spec(fields=[field("code", "varchar(3)")])

    result = validate(tmp_path, spec, "code\nABCD")

    assert result.rows_checked == 1
    assert "length 4 exceeds 3" in result.errors[0].message


def test_decimal_precision_and_scale_are_enforced(tmp_path: Path) -> None:
    spec = csv_spec(fields=[field("amount", "decimal(5,2)")])

    result = validate(tmp_path, spec, "amount\n1234.56")

    assert result.rows_checked == 1
    assert "does not fit declared precision/scale" in result.errors[0].message


@pytest.mark.parametrize(
    ("data_type", "value", "message"),
    [
        ("date", "not-a-date", "does not match data_type `date`"),
        ("timestamp", "not-a-timestamp", "does not match data_type `timestamp`"),
        ("boolean", "maybe", "must be boolean"),
        ("integer", "12.5", "must be an integer"),
    ],
)
def test_supported_types_reject_invalid_values(
    tmp_path: Path,
    data_type: str,
    value: str,
    message: str,
) -> None:
    spec = csv_spec(fields=[field("typed_value", data_type)])

    result = validate(tmp_path, spec, f"typed_value\n{value}")

    assert result.rows_checked == 1
    assert message in result.errors[0].message


def test_timestamp_data_type_rejects_values_without_timezone(tmp_path: Path) -> None:
    # Raw timestamp values must already carry timezone information.
    spec = csv_spec(fields=[field("event_at", "timestamp_tz")])

    result = validate(tmp_path, spec, "event_at\n2026-08-13T14:30:00")

    assert result.rows_checked == 1
    assert "must include a timezone" in result.errors[0].message


def test_timestamp_data_type_accepts_values_with_timezone(tmp_path: Path) -> None:
    spec = csv_spec(fields=[field("event_at", "timestamp_tz")])

    result = validate(tmp_path, spec, "event_at\n2026-08-13T14:30:00+10:00")

    assert result.errors == []
    assert result.rows_checked == 1


def test_parse_timestamp_rejects_missing_timezone_by_default(tmp_path: Path) -> None:
    spec = csv_spec(
        fields=[
            field(
                "event_at",
                "timestamp_tz",
                transforms=[
                    {
                        "type": "parse_timestamp",
                        "format": "%Y-%m-%d %H:%M:%S",
                    }
                ],
            )
        ]
    )

    result = validate(tmp_path, spec, "event_at\n2026-08-13 14:30:00")

    assert result.rows_checked == 1
    assert diagnostic_messages(result.errors) == ["timestamp transform result must include a timezone"]


@pytest.mark.parametrize("timezone_if_missing", ["Z", "UTC", "local"])
def test_parse_timestamp_can_apply_configured_timezone_when_missing(
    tmp_path: Path,
    timezone_if_missing: str,
) -> None:
    spec = csv_spec(
        fields=[
            field(
                "event_at",
                "timestamp_tz",
                transforms=[
                    {
                        "type": "parse_timestamp",
                        "format": "%Y-%m-%d %H:%M:%S",
                        "timezone_if_missing": timezone_if_missing,
                    }
                ],
            )
        ]
    )

    result = validate(tmp_path, spec, "event_at\n2026-08-13 14:30:00")

    assert result.errors == []
    assert result.rows_checked == 1


def test_custom_python_validator_failure_is_reported_on_the_row(tmp_path: Path) -> None:
    spec = csv_spec(
        fields=[
            field(
                "account_id",
                "varchar(20)",
                validations=[
                    {
                        "type": "custom",
                        "macro": "account_macros.validate_account_number",
                    }
                ],
            )
        ]
    )

    result = validate(tmp_path, spec, "account_id\nACCT000000000000")

    assert result.warnings == []
    assert diagnostic_messages(result.errors) == ["account number sequence must be greater than zero"]
    assert result.errors[0].location == "row 2, field `account_id`"
