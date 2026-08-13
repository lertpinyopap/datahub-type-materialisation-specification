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
    return f"""
    id: account_spec
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
        "valid_from_datetime": "datetime",
        "valid_to_datetime": "datetime",
        "business_data_hash": "varchar(64)",
        "audit_created_datetime": "datetime",
        "audit_last_changed_datetime": "datetime",
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


def test_csv_dbt_seed_requires_header_and_source_columns(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
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

    assert diagnostic_messages(diagnostics) == [
        "dbt_seed CSV sources require header: true",
        "dbt_seed CSV sources require field.source.column",
    ]


def test_csv_seed_block_requires_dbt_seed_load_method(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
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


def test_invalid_regex_validation_is_caught_at_parse_time(tmp_path: Path) -> None:
    _, diagnostics = parse_yaml(
        tmp_path,
        """
        id: account_spec
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
