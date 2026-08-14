"""Tests for semantic rules that sit beyond basic JSON Schema validation.

These checks cover reserved generated fields, SCD metadata contracts, CSV dialect
rules, data type support, and other cross-field specification constraints.
"""

from pathlib import Path
from textwrap import indent

from type_materialisation.cli import resolve_and_validate_spec
from type_materialisation.spec import GENERATED_METADATA_FIELD_TYPES, parse_sql_type
from tests.helpers import diagnostic_messages, write_spec


def complete_spec(extra: str = "", field_id: str = "account_id") -> str:
    extra_block = indent(extra.strip(), "    ") if extra.strip() else ""
    control_block = ""
    if "control_data:" not in extra:
        control_block = "    control_data:\n      change_type: scd1\n"
    return f"""
    id: account_spec
{control_block.rstrip()}
{extra_block}
    source:
      format: csv
      header: true
    target:
      id: account
      schema: business
      fields:
        - id: {field_id}
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
    """


def parse_yaml(tmp_path: Path, content: str):
    spec_path = write_spec(tmp_path, "spec", content)
    return resolve_and_validate_spec(spec_path, abstract=False)


def test_generated_metadata_field_type_contract_is_supported() -> None:
    # These are implementation-generated columns, but the spec owns their physical types.
    assert GENERATED_METADATA_FIELD_TYPES == {
        "is_current_flag": "varchar(1)",
        "is_deleted_flag": "varchar(1)",
        "valid_from_datetime": "timestamp_tz",
        "valid_to_datetime": "timestamp_tz",
        "business_data_hash": "varchar(64)",
        "audit_created_datetime": "timestamp_tz",
        "audit_last_changed_datetime": "timestamp_tz",
        "audit_data_process_key": "varchar(64)",
    }
    for data_type in GENERATED_METADATA_FIELD_TYPES.values():
        parse_sql_type(data_type)


def test_reserved_generated_field_names_are_rejected_case_insensitively(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(tmp_path, complete_spec(field_id="AUDIT_DATA_PROCESS_KEY"))

    assert any("reserved generated metadata field id" in message for message in diagnostic_messages(diagnostics))


def test_source_column_names_are_rejected_case_insensitively(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
            - id: account_number
              source:
                pos: 1
                column: ACCOUNT_ID
              data_type: varchar(20)
        """,
    )

    assert diagnostic_messages(diagnostics) == ["duplicates source column `account_id` case-insensitively"]
    assert diagnostics[0].location == "$.target.fields[1].source.column"


def test_csv_source_uses_python_dialect_names(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          delimiter: "|"
          quotechar: "'"
          lineterminator: "\\n"
          quoting: all
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
        """,
    )

    assert diagnostics == []


def test_csv_source_rejects_legacy_dialect_names(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          separator: "|"
          quote_char: "'"
          row_terminator: "\\n"
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
        """,
    )

    messages = diagnostic_messages(diagnostics)
    assert any("Additional properties are not allowed" in message for message in messages)


def test_csv_source_rejects_unsupported_quoting_values(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          quoting: none
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
        """,
    )

    assert any("'none' is not one of ['minimal', 'all']" in message for message in diagnostic_messages(diagnostics))


def test_concrete_control_data_requires_change_type(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          materialisation_type: table
        source:
          format: csv
          header: true
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
        """,
    )

    assert any("'change_type' is a required property" in message for message in diagnostic_messages(diagnostics))


def test_materialisation_type_only_supports_table(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          materialisation_type: view
          change_type: scd1
        source:
          format: csv
          header: true
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
        """,
    )

    assert any("'view' is not one of ['table']" in message for message in diagnostic_messages(diagnostics))


def test_csv_dbt_seed_without_header_accepts_position_only_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: false
          load_method: dbt_seed
          seed:
            file: account.csv
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
              data_type: varchar(20)
        """,
    )

    assert diagnostics == []


def test_csv_dbt_seed_with_header_requires_source_columns(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          load_method: dbt_seed
          seed:
            file: account.csv
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
              data_type: varchar(20)
        """,
    )

    assert diagnostic_messages(diagnostics) == [
        "dbt_seed CSV sources with a header require field.source.column",
    ]


def test_csv_seed_block_requires_dbt_seed_load_method(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          seed:
            file: account.csv
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
        """,
    )

    assert any("'load_method' is a required property" in message for message in diagnostic_messages(diagnostics))


def test_parse_timestamp_accepts_timezone_if_missing(tmp_path: Path) -> None:
    # Naive timestamp source strings need an explicit policy before they can become timestamp_tz values.
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
        target:
          id: account
          schema: business
          fields:
            - id: opened_at
              source:
                pos: 0
                column: opened_at
              data_type: timestamp_tz
              transforms:
                - type: parse_timestamp
                  format: "%Y-%m-%d %H:%M:%S"
                  timezone_if_missing: UTC
        """,
    )

    assert diagnostics == []


def test_parse_date_rejects_timezone_if_missing(tmp_path: Path) -> None:
    # The missing-timezone policy is only valid for parse_timestamp.
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
        target:
          id: account
          schema: business
          fields:
            - id: opened_on
              source:
                pos: 0
                column: opened_on
              data_type: date
              transforms:
                - type: parse_date
                  format: "%Y-%m-%d"
                  timezone_if_missing: UTC
        """,
    )

    assert diagnostics
    assert diagnostics[0].location == "$.target.fields.0.transforms.0"


def test_scd_business_key_must_reference_a_target_field(tmp_path: Path) -> None:
    # SCD rules should fail against field ids after inheritance and case-free matching.
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - missing_account_id
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == ["business key field does not exist in target.fields"]


def test_scd_business_data_hash_include_requires_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                business_data_hash:
                  mode: include
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == ["include mode requires at least one field"]


def test_scd_sparse_validity_mode_is_reserved_but_not_implemented(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                valid_from_to_mode: sparse
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == ["valid_from_to_mode `sparse` is not implemented yet"]


def test_scd_rejects_legacy_effective_from_key(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                effective_from:
                  mode: field
                  field: account_id
            """
        ),
    )

    assert any("Additional properties are not allowed" in message for message in diagnostic_messages(diagnostics))


def test_scd_delete_detection_never_is_valid(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                delete_detection:
                  mode: never
            """
        ),
    )

    assert diagnostics == []


def test_scd_delete_detection_field_uses_single_value(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                delete_detection:
                  mode: field
                  field: account_id
                  value: DELETED
            """
        ),
    )

    assert diagnostics == []


def test_scd_missing_from_source_rejects_field_valid_from_selection(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                delete_detection:
                  mode: missing_from_source
                valid_from_datetime:
                  valid_from_datetime_selection: field
                  field: account_id
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == [
        "delete_detection.mode `missing_from_source` is invalid when valid_from_datetime_selection is field"
    ]


def test_scd1_delete_detection_field_must_reference_target_field(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              scd:
                delete_detection:
                  mode: field
                  field: missing_status
                  value: DELETED
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == ["referenced field does not exist in target.fields"]


def test_invalid_regex_validation_is_caught_at_parse_time(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
              validations:
                - type: regex
                  pattern: "["
        """,
    )

    assert any("invalid regular expression" in message for message in diagnostic_messages(diagnostics))


def test_unsupported_jinja_expression_is_caught_at_parse_time(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: landing
          table: "{{ ref('not_allowed_here') }}"
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                column: account_id
              data_type: varchar(20)
        """,
    )

    assert diagnostic_messages(diagnostics) == ["unsupported Jinja expression `{{ ref('not_allowed_here') }}`"]


def test_supported_jinja_var_expression_accepts_double_quoted_arguments(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: landing
          table: '{{ var("source_table", "ACCOUNT_SOURCE") }}'
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                column: account_id
              data_type: varchar(20)
        """,
    )

    assert diagnostic_messages(diagnostics) == []
