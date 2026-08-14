"""Tests for resolving inherited type materialisation specifications.

These checks cover parent lookup, chained inheritance, merge rules, failure
cases, and making sure parse, validate, and dbt generation resolve parents first.
"""

from pathlib import Path

import pytest

from type_materialisation.cli import main, resolve_and_validate_spec
from type_materialisation.csv_validate import validate_csv_file
from type_materialisation.dbt_generate import GenerateDbtOptions, generate_dbt_project
from type_materialisation.inheritance import InheritanceError, resolve_spec
from tests.helpers import write_spec


def test_resolves_parent_from_same_directory(tmp_path: Path) -> None:
    # The child directory is the default inheritance search location.
    write_spec(
        tmp_path,
        "base",
        """
        id: base
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
    )
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: base
        target:
          id: child_account
        """,
    )

    resolved = resolve_spec(child)

    assert [path.name for path in resolved.chain] == ["base.yaml", "child.yaml"]
    assert resolved.spec["source"]["format"] == "csv"
    assert resolved.spec["target"]["id"] == "child_account"
    assert resolved.spec["target"]["schema"] == "business"
    assert "extends" not in resolved.spec


def test_resolves_parent_from_spec_path(tmp_path: Path) -> None:
    # Runtime search paths are used only when the parent is not beside the child.
    parent_dir = tmp_path / "parents"
    child_dir = tmp_path / "children"
    parent_dir.mkdir()
    child_dir.mkdir()
    write_spec(
        parent_dir,
        "base",
        """
        id: base
        target:
          id: inherited
          schema: business
          fields:
            - id: code
              source:
                column: code
              data_type: varchar(50)
        """,
    )
    child = write_spec(
        child_dir,
        "child",
        """
        id: child
        extends: base
        source:
          format: table
          schema: landing
          table: child_codes
        """,
    )

    resolved = resolve_spec(child, spec_paths=[parent_dir])

    assert resolved.spec["target"]["fields"][0]["id"] == "code"
    assert resolved.spec["source"]["table"] == "child_codes"


def test_resolves_parent_with_yml_suffix(tmp_path: Path) -> None:
    write_spec(
        tmp_path,
        "base",
        """
        id: base
        target:
          id: inherited
          schema: business
        """,
        suffix=".yml",
    )
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: base
        target:
          fields:
            - id: code
              source:
                column: code
              data_type: varchar(20)
        """,
    )

    resolved = resolve_spec(child)

    assert resolved.chain[0].name == "base.yml"
    assert resolved.spec["target"]["schema"] == "business"


def test_child_directory_parent_takes_precedence_over_spec_path(tmp_path: Path) -> None:
    # Local inheritance files should be stable even when shared paths contain the same id.
    parent_dir = tmp_path / "parents"
    child_dir = tmp_path / "children"
    parent_dir.mkdir()
    child_dir.mkdir()
    write_spec(
        parent_dir,
        "base",
        """
        id: base
        target:
          id: from_spec_path
          schema: shared
        """,
    )
    write_spec(
        child_dir,
        "base",
        """
        id: base
        target:
          id: from_child_dir
          schema: local
        """,
    )
    child = write_spec(
        child_dir,
        "child",
        """
        id: child
        extends: base
        """,
    )

    resolved = resolve_spec(child, spec_paths=[parent_dir])

    assert resolved.spec["target"]["id"] == "from_child_dir"
    assert resolved.spec["target"]["schema"] == "local"


def test_ambiguous_parent_candidates_in_same_directory_fail(tmp_path: Path) -> None:
    # Ambiguity in one directory is a specification error, not a tie to break.
    write_spec(tmp_path, "base", "id: base")
    write_spec(tmp_path, "BASE", "id: base", suffix=".yml")
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: base
        """,
    )

    with pytest.raises(InheritanceError, match="ambiguous"):
        resolve_spec(child)


def test_missing_parent_fails(tmp_path: Path) -> None:
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: missing_parent
        """,
    )

    with pytest.raises(InheritanceError, match="missing_parent"):
        resolve_spec(child)


def test_inheritance_cycle_fails(tmp_path: Path) -> None:
    # Cycles otherwise recurse until the process fails in a much less helpful way.
    write_spec(
        tmp_path,
        "a",
        """
        id: a
        extends: b
        """,
    )
    b = write_spec(
        tmp_path,
        "b",
        """
        id: b
        extends: a
        """,
    )

    with pytest.raises(InheritanceError, match="cycle"):
        resolve_spec(b)


def test_child_overlays_nested_mappings_and_replaces_non_field_lists(tmp_path: Path) -> None:
    write_spec(
        tmp_path,
        "base",
        """
        id: base
        control_data:
          materialisation_type: table
          change_type: scd1
          failure_mode: quarantine_row
        source:
          format: csv
          header: true
          location:
            schema: AD_HOC
            stage: "@parent_stage"
            filename: parent.csv
        target:
          id: account
          schema: business
          fields:
            - id: account_status
              source:
                pos: 0
                column: account_status
              data_type: varchar(20)
              validations:
                - type: allowed_values
                  values:
                    - ACTIVE
                    - CLOSED
        """,
    )
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: base
        source:
          location:
            filename: child.csv
        target:
          fields:
            - id: account_status
              validations:
                - type: allowed_values
                  values:
                    - ACTIVE
        """,
    )

    resolved = resolve_spec(child).spec

    assert resolved["source"]["location"] == {
        "schema": "AD_HOC",
        "stage": "@parent_stage",
        "filename": "child.csv",
    }
    assert resolved["target"]["fields"][0]["validations"][0]["values"] == ["ACTIVE"]


def test_multiple_inheritance_layers_overlay_oldest_to_child(tmp_path: Path) -> None:
    # Chained inheritance must apply overlays from the oldest ancestor forward.
    write_spec(
        tmp_path,
        "grandparent",
        """
        id: grandparent
        source:
          format: csv
          header: true
          location:
            schema: AD_HOC
            stage: "@grandparent_stage"
            filename: grandparent.csv
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(50)
        """,
    )
    write_spec(
        tmp_path,
        "parent",
        """
        id: parent
        extends: grandparent
        source:
          location:
            stage: "@parent_stage"
        target:
          fields:
            - id: account_name
              source:
                pos: 1
                column: account_name
              data_type: varchar(255)
        """,
    )
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: parent
        source:
          location:
            filename: child.csv
        target:
          id: child_account
        """,
    )

    resolved = resolve_spec(child)

    assert [path.name for path in resolved.chain] == ["grandparent.yaml", "parent.yaml", "child.yaml"]
    assert resolved.spec["source"]["location"] == {
        "schema": "AD_HOC",
        "stage": "@parent_stage",
        "filename": "child.csv",
    }
    assert resolved.spec["target"]["id"] == "child_account"
    assert [field["id"] for field in resolved.spec["target"]["fields"]] == ["account_id", "account_name"]


def test_fields_overlay_by_id_case_insensitively_and_preserve_parent_order(tmp_path: Path) -> None:
    # Field ids inherit using the same case-free identity rules as database ids.
    write_spec(
        tmp_path,
        "base",
        """
        id: base
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(50)
              nullable: false
              unique: true
            - id: account_name
              source:
                pos: 1
                column: account_name
              data_type: varchar(255)
        """,
    )
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: BASE
        target:
          fields:
            - id: ACCOUNT_ID
              data_type: varchar(20)
            - id: opened_on
              source:
                pos: 2
                column: opened_on
              data_type: date
        """,
    )

    resolved_fields = resolve_spec(child).spec["target"]["fields"]

    assert [field["id"] for field in resolved_fields] == ["ACCOUNT_ID", "account_name", "opened_on"]
    assert resolved_fields[0]["source"] == {"pos": 0, "column": "account_id"}
    assert resolved_fields[0]["data_type"] == "varchar(20)"
    assert resolved_fields[0]["nullable"] is False
    assert resolved_fields[0]["unique"] is True


def test_parent_id_must_match_requested_id(tmp_path: Path) -> None:
    write_spec(
        tmp_path,
        "base",
        """
        id: not_base
        """,
    )
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: base
        """,
    )

    with pytest.raises(InheritanceError, match="not_base"):
        resolve_spec(child)


def test_parse_resolves_before_concrete_validation(tmp_path: Path) -> None:
    # The child is incomplete until inheritance has supplied source and fields.
    write_spec(
        tmp_path,
        "base",
        """
        id: base
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
    )
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: base
        target:
          table_name: child_account
        """,
    )

    resolved, diagnostics = resolve_and_validate_spec(child, abstract=False)

    assert diagnostics == []
    assert resolved is not None
    assert resolved["target"]["table_name"] == "child_account"


def test_cli_parse_validate_and_generate_fail_when_parent_is_missing(tmp_path: Path) -> None:
    # All user-facing commands must fail before doing work against an unresolved child.
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: missing_parent
        """,
    )
    csv_path = tmp_path / "input.csv"
    csv_path.write_text("id\n1\n", encoding="utf-8")

    assert main(["parse", "--spec", str(child)]) == 1
    assert main(["validate", "--spec", str(child), "--input-file", str(csv_path)]) == 1
    assert main(["generate-dbt", "--spec", str(child), "--output-dir", str(tmp_path / "dbt")]) == 1


def test_csv_validation_resolves_inheritance_before_validating(tmp_path: Path) -> None:
    # Direct library callers get the same resolved spec behavior as the CLI.
    write_spec(
        tmp_path,
        "base",
        """
        id: base
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
              nullable: false
        """,
    )
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: base
        target:
          table_name: child_account
        """,
    )
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id\nA-001\n", encoding="utf-8")

    result = validate_csv_file(child, csv_path)

    assert result.errors == []
    assert result.rows_checked == 1


def test_dbt_generation_resolves_inheritance_before_generating(tmp_path: Path) -> None:
    # Generation should use the resolved target, not the sparse child document.
    write_spec(
        tmp_path,
        "base",
        """
        id: base
        control_data:
          materialisation_type: table
          change_type: scd1
        source:
          format: csv
          header: true
          location:
            schema: AD_HOC
            stage: "@csv_stage"
            filename: account.csv
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
    child = write_spec(
        tmp_path,
        "child",
        """
        id: child
        extends: base
        target:
          id: child_account
          table_name: child_account_table
        """,
    )
    output_dir = tmp_path / "generated"

    result = generate_dbt_project(GenerateDbtOptions(spec_path=child, output_dir=output_dir))

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "child_account.sql").read_text(encoding="utf-8")
    assert "alias='CHILD_ACCOUNT_TABLE'" in model_sql
    assert "cast(ACCOUNT_ID as varchar(20)) as ACCOUNT_ID" in model_sql
    assert (
        "cast('{{ var(\"audit_data_process_key\", \"manual\") }}' as varchar(64)) as AUDIT_DATA_PROCESS_KEY"
        in model_sql
    )
    assert "cast(current_timestamp() as timestamp_tz) as AUDIT_CREATED_DATETIME" in model_sql
    assert "cast(current_timestamp() as timestamp_tz) as AUDIT_LAST_CHANGED_DATETIME" in model_sql
