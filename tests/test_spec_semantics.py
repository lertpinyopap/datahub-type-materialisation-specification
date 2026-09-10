"""Tests for semantic rules that sit beyond basic JSON Schema validation.

These checks cover reserved generated fields, SCD metadata contracts, CSV dialect
rules, data type support, and other cross-field specification constraints.
"""

import re
from pathlib import Path
from textwrap import dedent, indent

import pytest

from type_materialisation.cli import resolve_and_validate_spec
from type_materialisation.spec import (
    BUSINESS_KEY_DATA_TYPE,
    GENERATED_METADATA_FIELD_TYPES,
    SURROGATE_KEY_DATA_TYPE,
    parse_sql_type,
)
from tests.helpers import diagnostic_messages, write_spec


def complete_spec(extra: str = "", field_id: str = "account_id") -> str:
    extra_block = indent(extra.strip(), "    ") if extra.strip() else ""
    control_block = ""
    if "control_data:" not in extra:
        control_block = _control_data_block("", field_id=field_id)
    else:
        extra_block = _control_data_block(extra, field_id=field_id)
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


def _control_data_block(extra_control: str, *, field_id: str = "account_id") -> str:
    if extra_control.strip():
        block = dedent(extra_control).strip()
    else:
        block = "control_data:\n  change_type: scd1"
    if "business_key:" not in block:
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if line.strip().startswith("change_type:"):
                prefix = line[: len(line) - len(line.lstrip())]
                lines[index + 1:index + 1] = [
                    f"{prefix}business_key:",
                    f"{prefix}  fields:",
                    f"{prefix}    - {field_id}",
                ]
                break
        block = "\n".join(lines)
    return indent(block, "    ")


def parse_yaml(tmp_path: Path, content: str, *, auto_business_key: bool = True):
    if auto_business_key:
        content = _with_default_business_key(content)
    spec_path = write_spec(tmp_path, "spec", content)
    return resolve_and_validate_spec(spec_path, abstract=False)


def _with_default_business_key(content: str) -> str:
    if "business_key:" in content or "control_data:" not in content:
        return content
    match = re.search(r"(?m)^\s*-\s+id:\s+([A-Za-z][A-Za-z0-9_-]*)\s*$", content)
    field_id = match.group(1) if match else "account_id"
    lines = dedent(content).splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("change_type:"):
            prefix = line[: len(line) - len(line.lstrip())]
            lines[index + 1:index + 1] = [
                f"{prefix}business_key:",
                f"{prefix}  fields:",
                f"{prefix}    - {field_id}",
            ]
            break
    return "\n".join(lines)


def test_generated_metadata_field_type_contract_is_supported() -> None:
    # These are implementation-generated columns, but the spec owns their physical types.
    assert BUSINESS_KEY_DATA_TYPE == "varchar"
    assert SURROGATE_KEY_DATA_TYPE == "varchar(36)"
    assert GENERATED_METADATA_FIELD_TYPES == {
        "is_current_flag": "varchar(1)",
        "is_deleted_flag": "varchar(1)",
        "valid_from_datetime": "timestamp_tz",
        "valid_to_datetime": "timestamp_tz",
        "business_data_hash": "varchar(64)",
        "audit_created_datetime": "timestamp_tz",
        "audit_last_changed_datetime": "timestamp_tz",
        "audit_data_process_key": "varchar(256)",
    }
    for data_type in [SURROGATE_KEY_DATA_TYPE, *GENERATED_METADATA_FIELD_TYPES.values()]:
        parse_sql_type(data_type)


def test_reserved_generated_field_names_are_rejected_case_insensitively(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(tmp_path, complete_spec(field_id="AUDIT_DATA_PROCESS_KEY"))

    assert any("reserved generated metadata field id" in message for message in diagnostic_messages(diagnostics))


def test_failure_mode_uses_fail_load_enum(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              failure_mode: fail_load
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == []


def test_failure_mode_rejects_old_fail_file_enum(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              failure_mode: fail_file
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == ["'fail_file' is not one of ['fail_load', 'quarantine_row']"]


def test_validation_enabled_accepts_boolean(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              validation_enabled: false
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == []


def test_validation_enabled_rejects_non_boolean(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              validation_enabled: "false"
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == ["'false' is not of type 'boolean'"]


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


def test_table_snowflake_path_allows_reusing_payload_column(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                column: payload
                snowflake_path: account.id
              data_type: varchar(20)
            - id: account_name
              source:
                column: payload
                snowflake_path: account.profile.name
              data_type: varchar(255)
        """,
    )

    assert diagnostics == []


def test_fixed_value_source_is_allowed_without_column_or_position(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: load_status_flag
              source:
                fixed_value: "Y"
              data_type: varchar(1)
        """,
    )

    assert diagnostics == []


def test_fixed_value_source_rejects_other_selectors(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - load_status_flag
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: load_status_flag
              source:
                column: payload
                fixed_value: "Y"
              data_type: varchar(1)
        """,
        auto_business_key=False,
    )

    assert "field.source.fixed_value cannot be combined with field.source.column" in diagnostic_messages(diagnostics)


def test_default_value_source_is_allowed_with_column_selector(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: country_code
              source:
                column: country_code
                default_value: "UNKNOWN"
              data_type: varchar(20)
        """,
    )

    assert diagnostics == []


def test_standalone_default_value_source_is_allowed_for_table_source(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: external_identification_type
              source:
                default_value: "AMID"
              data_type: varchar(20)
        """,
    )

    assert diagnostics == []


def test_default_value_source_rejects_fixed_value(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - country_code
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: country_code
              source:
                fixed_value: "Y"
                default_value: "UNKNOWN"
              data_type: varchar(20)
        """,
        auto_business_key=False,
    )

    assert "field.source.default_value cannot be combined with field.source.fixed_value" in diagnostic_messages(diagnostics)


def test_default_from_field_source_is_allowed_with_column_selector(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - customer_id
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: customer_id
              source:
                column: customer_id
              data_type: varchar(20)
            - id: clv_id
              source:
                default_from_field: customer_id
              data_type: varchar(20)
        """,
        auto_business_key=False,
    )

    assert diagnostics == []


def test_default_from_field_source_rejects_unknown_field(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - customer_id
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: customer_id
              source:
                column: customer_id
              data_type: varchar(20)
            - id: clv_id
              source:
                column: clv_id
                default_from_field: missing_customer_id
              data_type: varchar(20)
        """,
        auto_business_key=False,
    )

    assert "field.source.default_from_field must reference another target field" in diagnostic_messages(diagnostics)


def test_default_from_field_source_rejects_same_field(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - customer_id
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: customer_id
              source:
                column: customer_id
                default_from_field: customer_id
              data_type: varchar(20)
        """,
        auto_business_key=False,
    )

    assert "field.source.default_from_field cannot reference the same field" in diagnostic_messages(diagnostics)


def test_default_from_field_source_rejects_fixed_value(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - customer_id
        source:
          format: table
          schema: raw
          table: landed_events
        target:
          id: account
          schema: business
          fields:
            - id: customer_id
              source:
                column: customer_id
              data_type: varchar(20)
            - id: clv_id
              source:
                fixed_value: "Y"
                default_from_field: customer_id
              data_type: varchar(20)
        """,
        auto_business_key=False,
    )

    assert "field.source.default_from_field cannot be combined with field.source.fixed_value" in diagnostic_messages(diagnostics)


def test_default_from_field_source_rejects_csv_source(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - customer_id
        source:
          format: csv
          load_method: dbt_seed
          header: true
          seed:
            file: account.csv
            name: account
        target:
          id: account
          schema: business
          fields:
            - id: customer_id
              source:
                column: customer_id
              data_type: varchar(20)
            - id: clv_id
              source:
                column: clv_id
                default_from_field: customer_id
              data_type: varchar(20)
        """,
        auto_business_key=False,
    )

    assert "field.source.default_from_field is supported only for table sources" in diagnostic_messages(diagnostics)


def test_snowflake_path_is_rejected_for_csv_source(tmp_path: Path) -> None:
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
                column: payload
                snowflake_path: account.id
              data_type: varchar(20)
        """,
    )

    assert "field.source.snowflake_path is only valid for table sources" in diagnostic_messages(diagnostics)


def test_table_flatten_aliases_are_rejected_case_insensitively(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: order_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: raw
          table: landed_events
          flatten:
            - column: payload
              path: orders
              alias: item
            - column: payload
              path: shipments
              alias: ITEM
        target:
          id: order
          schema: business
          fields:
            - id: order_id
              source:
                column: item
                snowflake_path: id
              data_type: varchar(20)
        """,
    )

    assert "duplicates source.flatten.alias `item` case-insensitively" in diagnostic_messages(diagnostics)


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
        "dbt_seed CSV sources with a header require field.source.column, field.source.macro, or field.source.fixed_value",
    ]


def test_csv_dbt_seed_with_header_accepts_source_macro(tmp_path: Path) -> None:
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
            - id: customer_status_key
              source:
                macro: lookup_macros.customer_status_key
                args:
                  source_code_expression: source_query.STATUS_CODE
              data_type: varchar(64)
        """,
    )

    assert diagnostics == []


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


def test_parse_timestamp_accepts_time_if_missing(tmp_path: Path) -> None:
    # Date-only timestamp source strings can explicitly choose the generated time of day.
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
                  format: "%d/%m/%Y"
                  timezone_if_missing: UTC
                  time_if_missing: end_of_day
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


def test_scd2_auto_requires_insert_time(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_auto
            """
        ),
    )

    assert "`scd.insert_time` is required when `change_type` is scd2_auto" in diagnostic_messages(diagnostics)


def test_concrete_spec_requires_business_key(tmp_path: Path) -> None:
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
        """,
        auto_business_key=False,
    )

    assert "`business_key` is required" in diagnostic_messages(diagnostics)


def test_business_key_fields_must_exist(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              business_key:
                fields:
                  - missing_account_id
            """
        ),
    )

    assert "business key field does not exist in target.fields" in diagnostic_messages(diagnostics)


def test_business_data_hash_control_accepts_include_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              business_data_hash:
                business_data_hash_mode: include
                fields:
                  - account_id
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == []


def test_business_data_hash_include_requires_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              business_data_hash:
                business_data_hash_mode: include
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == ["include mode requires at least one field"]


def test_business_data_hash_include_rejects_generated_business_key(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              business_data_hash:
                business_data_hash_mode: include
                fields:
                  - account_business_key
            """
        ),
    )

    assert diagnostic_messages(diagnostics) == [
        "business data hash include field must not reference generated keys or SCD2 metadata fields"
    ]


def test_business_data_hash_include_rejects_scd2_manual_metadata_field(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd2_manual
          business_key:
            fields:
              - account_id
          business_data_hash:
            business_data_hash_mode: include
            fields:
              - valid_from_datetime
          scd:
            update_mode: upsert
            update_key:
              fields:
                - valid_from_datetime
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
            - id: valid_from_datetime
              source:
                pos: 1
                column: VALID_FROM_DATETIME
              data_type: timestamp_tz
            - id: valid_to_datetime
              source:
                pos: 2
                column: VALID_TO_DATETIME
              data_type: timestamp_tz
            - id: is_current_flag
              source:
                pos: 3
                column: IS_CURRENT_FLAG
              data_type: varchar(1)
            - id: is_deleted_flag
              source:
                pos: 4
                column: IS_DELETED_FLAG
              data_type: varchar(1)
        """,
        auto_business_key=False,
    )

    assert diagnostic_messages(diagnostics) == [
        "business data hash include field must not reference generated keys or SCD2 metadata fields"
    ]


def test_default_business_key_name_must_not_collide_with_target_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(field_id="account_business_key"),
    )

    messages = diagnostic_messages(diagnostics)
    assert "uses a reserved generated metadata field id" in messages
    assert "business key name collides with a target field or generated metadata field" in messages


def test_default_surrogate_key_name_must_not_collide_with_target_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(field_id="account_key"),
    )

    assert "uses a reserved generated metadata field id" in diagnostic_messages(diagnostics)


def test_surrogate_key_can_be_disabled(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              skip_surrogate_key: true
            """,
            field_id="account_key",
        ),
    )

    assert diagnostics == []


def test_business_key_can_be_disabled(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          skip_business_key: true
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
        auto_business_key=False,
    )

    assert diagnostics == []


def test_business_key_fields_must_not_reference_surrogate_key(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - account_key
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

    assert "business key field must not reference generated surrogate key" in diagnostic_messages(diagnostics)


def test_scd2_auto_accepts_insert_time_and_business_key(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd2_auto
          business_key:
            fields:
              - account_id
          scd:
            insert_time: "{{ var('insert_time') }}"
            scd2_auto_from_sot: false
            scd2_validation: sparse
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
              unique: true
        """,
    )

    assert diagnostics == []


def test_control_data_accepts_truncate_before_load_for_scd1(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              truncate_before_load: true
            """
        ),
    )

    assert diagnostics == []


def test_control_data_accepts_truncate_before_load_for_scd2_auto(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_auto
              truncate_before_load: false
              scd:
                insert_time: "{{ var('insert_time') }}"
            """
        ),
    )

    assert diagnostics == []


def test_scd2_validation_rejects_unknown_mode(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
                scd2_validation: strict
            """
        ),
    )

    assert "'strict' is not one of ['continuous', 'sparse']" in diagnostic_messages(diagnostics)


def test_scd2_validation_enabled_accepts_boolean_for_scd2_auto(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
                scd2_validation_enabled: false
            """
        ),
    )

    assert diagnostics == []


def test_scd2_validation_enabled_rejects_non_boolean(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
                scd2_validation_enabled: "false"
            """
        ),
    )

    assert "'false' is not of type 'boolean'" in diagnostic_messages(diagnostics)


def test_scd2_validation_is_only_valid_for_generated_scd2(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              scd:
                scd2_validation: sparse
            """
        ),
    )

    assert "`scd2_validation` is only valid when `change_type` is scd2_auto or scd2_derived" in diagnostic_messages(diagnostics)


def test_scd2_auto_from_sot_is_only_valid_for_scd2_auto(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd1
              scd:
                scd2_auto_from_sot: false
            """
        ),
    )

    assert "`scd2_auto_from_sot` is only valid when `change_type` is scd2_auto" in diagnostic_messages(diagnostics)


def test_scd_rejects_legacy_effective_from_key(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
                effective_from:
                  mode: field
                  field: account_id
            """
        ),
    )

    assert any("Additional properties are not allowed" in message for message in diagnostic_messages(diagnostics))


def test_scd2_auto_rejects_field_delete_detection(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
                delete_detection:
                  mode: field
                  field: account_id
                  value: DELETED
            """
        ),
    )

    assert "`delete_detection.mode = field` is only valid when `change_type` is scd1" in diagnostic_messages(diagnostics)


def test_scd2_auto_rejects_update_mode(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
                update_mode: upsert
            """
        ),
    )

    assert "`update_mode` is only valid when `change_type` is scd2_manual" in diagnostic_messages(diagnostics)


def test_scd2_manual_requires_copied_scd_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_manual
            """
        ),
    )

    messages = diagnostic_messages(diagnostics)
    assert "`scd2_manual` requires target field `valid_from_datetime`" in messages
    assert "`scd2_manual` requires target field `valid_to_datetime`" in messages
    assert "`scd2_manual` requires target field `is_current_flag`" in messages
    assert "`scd2_manual` requires target field `is_deleted_flag`" in messages


def test_scd2_manual_accepts_copied_scd_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd2_manual
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
            - id: valid_from_datetime
              source:
                pos: 1
                column: VALID_FROM_DATETIME
              data_type: timestamp_tz
            - id: valid_to_datetime
              source:
                pos: 2
                column: VALID_TO_DATETIME
              data_type: timestamp_tz
            - id: is_current_flag
              source:
                pos: 3
                column: IS_CURRENT_FLAG
              data_type: varchar(1)
            - id: is_deleted_flag
              source:
                pos: 4
                column: IS_DELETED_FLAG
              data_type: varchar(1)
        """,
    )

    assert diagnostics == []


def test_scd2_manual_scd_fields_must_use_canonical_source_columns(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd2_manual
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
            - id: valid_from_datetime
              source:
                pos: 1
                column: source_valid_from
              data_type: timestamp_tz
            - id: valid_to_datetime
              source:
                pos: 2
                column: VALID_TO_DATETIME
              data_type: timestamp_tz
            - id: is_current_flag
              source:
                pos: 3
                column: IS_CURRENT_FLAG
              data_type: varchar(1)
            - id: is_deleted_flag
              source:
                pos: 4
                column: IS_DELETED_FLAG
              data_type: varchar(1)
        """,
    )

    assert "`valid_from_datetime` must map from source column `VALID_FROM_DATETIME` for `scd2_manual`" in diagnostic_messages(diagnostics)


def test_scd2_manual_accepts_update_mode(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd2_manual
          scd:
            update_mode: upsert
            update_key:
              fields:
                - valid_from_datetime
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
            - id: valid_from_datetime
              source:
                pos: 1
                column: VALID_FROM_DATETIME
              data_type: timestamp_tz
            - id: valid_to_datetime
              source:
                pos: 2
                column: VALID_TO_DATETIME
              data_type: timestamp_tz
            - id: is_current_flag
              source:
                pos: 3
                column: IS_CURRENT_FLAG
              data_type: varchar(1)
            - id: is_deleted_flag
              source:
                pos: 4
                column: IS_DELETED_FLAG
              data_type: varchar(1)
        """,
    )

    assert diagnostics == []


def test_scd2_manual_rejects_update_key_without_upsert(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd2_manual
          scd:
            update_key:
              fields:
                - valid_from_datetime
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
            - id: valid_from_datetime
              source:
                pos: 1
                column: VALID_FROM_DATETIME
              data_type: timestamp_tz
            - id: valid_to_datetime
              source:
                pos: 2
                column: VALID_TO_DATETIME
              data_type: timestamp_tz
            - id: is_current_flag
              source:
                pos: 3
                column: IS_CURRENT_FLAG
              data_type: varchar(1)
            - id: is_deleted_flag
              source:
                pos: 4
                column: IS_DELETED_FLAG
              data_type: varchar(1)
        """,
    )

    assert "`scd.update_key` is only valid when `scd.update_mode` is upsert" in diagnostic_messages(diagnostics)


def test_scd2_manual_update_key_fields_must_reference_target_fields(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd2_manual
          scd:
            update_mode: upsert
            update_key:
              fields:
                - missing_field
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
            - id: valid_from_datetime
              source:
                pos: 1
                column: VALID_FROM_DATETIME
              data_type: timestamp_tz
            - id: valid_to_datetime
              source:
                pos: 2
                column: VALID_TO_DATETIME
              data_type: timestamp_tz
            - id: is_current_flag
              source:
                pos: 3
                column: IS_CURRENT_FLAG
              data_type: varchar(1)
            - id: is_deleted_flag
              source:
                pos: 4
                column: IS_DELETED_FLAG
              data_type: varchar(1)
        """,
    )

    assert "update key field does not exist in target.fields" in diagnostic_messages(diagnostics)


def test_scd2_manual_rejects_non_manual_scd_parameters(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        complete_spec(
            """
            control_data:
              change_type: scd2_manual
              scd:
                insert_time: "{{ var('insert_time') }}"
            """
        ),
    )

    assert "`scd2_manual` supports only `scd.update_mode` and `scd.update_key`" in diagnostic_messages(diagnostics)


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


def test_table_source_query_accepts_full_select_with_supported_jinja(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          query: |
            select account_id
            from landing.account_source
            where load_batch_id = '{{ var('load_batch_id') }}'
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


def test_csv_source_rejects_query(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: csv_spec
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          query: "select * from landing.account_source"
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

    assert any("query" in message for message in diagnostic_messages(diagnostics))


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("delete from account", "table source query must start with SELECT or WITH"),
        ("select * from account; drop table account", "table source query must be a single SQL query without `;`"),
    ],
)
def test_table_source_query_rejects_non_select_or_statement(
    tmp_path: Path,
    query: str,
    message: str,
) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        f"""
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          query: "{query}"
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

    assert message in diagnostic_messages(diagnostics)


def test_scd2_derived_allows_omitting_declared_valid_to_datetime(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
        control_data:
          change_type: scd2_derived
          business_key:
            fields:
              - account_id
          business_data_hash:
            business_data_hash_mode: include
            fields:
              - account_name
        source:
          format: table
          query: select * from landing.account_source
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                column: account_id
              data_type: varchar(20)
            - id: account_name
              source:
                column: account_name
              data_type: varchar(255)
            - id: valid_from_datetime
              source:
                column: SOURCE_EFFECTIVE_FROM_DATETIME
              data_type: timestamp_tz
        """,
    )

    assert diagnostics == []