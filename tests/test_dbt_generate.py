"""Tests for generating a runnable dbt project from a materialisation spec.

These checks cover CSV and table source models, generated model SQL, job hooks,
quarantine models, generated unit tests, and unsupported feature reporting.
"""

import hashlib
import re
from pathlib import Path
from textwrap import dedent, indent

from type_materialisation.schema import load_yaml
from type_materialisation.dbt_generate import GenerateDbtOptions, generate_dbt_project
from tests.helpers import diagnostic_messages, write_spec


def csv_generation_spec(
    extra_location: str = "",
    extra_control: str = "",
    fields: str | None = None,
) -> str:
    field_block = fields or """
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
    """
    control_block = _control_data_block(extra_control, field_id=_first_field_id(field_block))
    location_block = indent(extra_location.strip(), "        ") if extra_location.strip() else ""
    return f"""
    id: account_csv
{control_block}
    source:
      format: csv
      header: true
      location:
        schema: AD_HOC
        stage: "@csv_stage"
        filename: account.csv
{location_block}
    target:
      id: account
      schema: business
      fields:
    {field_block}
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


def _first_field_id(content: str) -> str:
    match = re.search(r"(?m)^\s*-\s+id:\s+([A-Za-z][A-Za-z0-9_-]*)\s*$", content)
    return match.group(1) if match else "account_id"


def _with_default_business_key(content: str) -> str:
    if "business_key:" in content or "control_data:" not in content:
        return content
    field_id = _first_field_id(content)
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


def generate(tmp_path: Path, spec_content: str, *, auto_business_key: bool = True, **options):
    if auto_business_key:
        spec_content = _with_default_business_key(spec_content)
    spec_path = write_spec(tmp_path, "spec", spec_content)
    output_dir = tmp_path / "generated"
    result = generate_dbt_project(GenerateDbtOptions(spec_path=spec_path, output_dir=output_dir, **options))
    return result, output_dir


def test_generation_fails_for_non_empty_output_directory(tmp_path: Path) -> None:
    spec_path = write_spec(tmp_path, "spec", csv_generation_spec())
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    (output_dir / "stale_model.sql").write_text("select 1\n", encoding="utf-8")

    result = generate_dbt_project(GenerateDbtOptions(spec_path=spec_path, output_dir=output_dir))

    assert len(result.errors) == 1
    assert result.errors[0].location == str(output_dir)
    assert "output directory already exists and is not empty" in result.errors[0].message


def test_generation_rejects_non_table_materialisation_type(tmp_path: Path) -> None:
    result, _ = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              materialisation_type: view
              change_type: scd1
            """
        ),
    )

    assert diagnostic_messages(result.errors) == ["dbt generation supports materialisation_type = table"]
    assert result.errors[0].location == "$.control_data.materialisation_type"


def test_csv_stage_location_omits_database_when_not_supplied(tmp_path: Path) -> None:
    # Omitted databases are resolved by dbt/Snowflake context, not by the generator.
    result, output_dir = generate(tmp_path, csv_generation_spec())

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "from @AD_HOC.CSV_STAGE/account.csv" in source_sql
    assert "schema=var('tms_staging_schema', 'INTERMEDIATE') | upper" in source_sql


def test_control_data_staging_schema_overrides_generated_staging_defaults(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              staging_schema: scratch
              failure_mode: quarantine_row
            """
        ),
    )

    assert result.errors == []
    project = load_yaml(output_dir / "dbt_project.yml")
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    quarantine_sql = (output_dir / "models" / "generated" / "account__quarantine.sql").read_text(encoding="utf-8")
    assert "schema=var('tms_staging_schema', 'SCRATCH') | upper" in source_sql
    assert "schema=var('tms_staging_schema', 'SCRATCH') | upper" in quarantine_sql
    assert "{{ var(\"tms_staging_schema\", \"SCRATCH\") | upper }}.ACCOUNT__QUARANTINE" in project["on-run-start"][1]


def test_csv_stage_location_includes_database_when_supplied(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec(extra_location="database: raw"))

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "from @RAW.AD_HOC.CSV_STAGE/account.csv" in source_sql


def test_csv_stage_override_wins_over_spec_location(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec(), csv_stage="raw.override_stage/override.csv")

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "from @RAW.OVERRIDE_STAGE/override.csv" in source_sql
    assert "csv_stage/account.csv" not in source_sql


def test_csv_dbt_seed_generation_copies_seed_and_reads_from_ref(tmp_path: Path) -> None:
    seed_file = tmp_path / "account_seed_input.csv"
    seed_file.write_text("account_id,account_name\nACCT000000000001,Acme Trading\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        f"""
        id: account_csv
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          load_method: dbt_seed
          seed:
            file: {seed_file}
            name: account_seed
            schema: TMP
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
            - id: account_name
              source:
                pos: 1
                column: account_name
              data_type: varchar(255)
        """,
    )

    assert result.errors == []
    assert (output_dir / "seeds" / "account_seed.csv").read_text(encoding="utf-8") == seed_file.read_text(encoding="utf-8")
    project = load_yaml(output_dir / "dbt_project.yml")
    seed_config = project["seeds"]["type_materialisation_generated"]["account_seed"]
    assert seed_config["+schema"] == "TMP"
    assert seed_config["+quote_columns"] is False
    assert seed_config["+alias"] == "ACCOUNT_SEED"
    assert seed_config["+column_types"] == {"ACCOUNT_ID": "varchar", "ACCOUNT_NAME": "varchar"}
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "from {{ ref('account_seed') }}" in source_sql
    assert "cast(ACCOUNT_ID as string) as ACCOUNT_ID" in source_sql
    assert "@csv_stage" not in source_sql


def test_csv_dbt_seed_file_can_use_generation_var(monkeypatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seed_file = run_dir / "account_seed_input.csv"
    seed_file.write_text("account_id\nACCT000000000001\n", encoding="utf-8")
    monkeypatch.chdir(run_dir)
    result, output_dir = generate(
        tmp_path,
        """
        id: account_csv
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          load_method: dbt_seed
          seed:
            file: "{{ tms_var('seed_file') }}"
            name: account_seed
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
        vars={"seed_file": seed_file.name},
    )

    assert result.errors == []
    assert (output_dir / "seeds" / "account_seed.csv").read_text(encoding="utf-8") == seed_file.read_text(encoding="utf-8")
    project = load_yaml(output_dir / "dbt_project.yml")
    seed_config = project["seeds"]["type_materialisation_generated"]["account_seed"]
    assert seed_config["+schema"] == "{{ var('tms_staging_schema', 'INTERMEDIATE') | upper }}"


def test_csv_dbt_seed_file_can_use_generation_var_default(monkeypatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seed_file = run_dir / "account_seed_input.csv"
    seed_file.write_text("account_id\nACCT000000000001\n", encoding="utf-8")
    monkeypatch.chdir(run_dir)
    result, output_dir = generate(
        tmp_path,
        """
        id: account_csv
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          load_method: dbt_seed
          seed:
            file: "{{ tms_var('seed_file', 'account_seed_input.csv') }}"
            name: account_seed
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

    assert result.errors == []
    assert (output_dir / "seeds" / "account_seed.csv").read_text(encoding="utf-8") == seed_file.read_text(encoding="utf-8")


def test_generation_resolves_tms_vars_before_generating_project(tmp_path: Path) -> None:
    seed_file = tmp_path / "account_seed_input.csv"
    seed_file.write_text("account_id\nACCT000000000001\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        """
        id: account_csv
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          load_method: dbt_seed
          seed:
            file: "{{ tms_var('seed_file') }}"
            name: account_seed
            schema: "{{ tms_var('seed_schema', 'TMP') }}"
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
        vars={"seed_file": str(seed_file), "seed_schema": "SANDBOX"},
    )

    assert result.errors == []
    project = load_yaml(output_dir / "dbt_project.yml")
    seed_config = project["seeds"]["type_materialisation_generated"]["account_seed"]
    assert seed_config["+schema"] == "SANDBOX"


def test_generation_fails_when_required_tms_var_is_missing(tmp_path: Path) -> None:
    result, _ = generate(
        tmp_path,
        """
        id: account_csv
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          load_method: dbt_seed
          seed:
            file: "{{ tms_var('seed_file') }}"
            name: account_seed
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

    assert diagnostic_messages(result.errors) == ["variable `seed_file` was not provided"]
    assert result.errors[0].location == "$.source.seed.file"


def test_csv_dbt_seed_without_header_synthesizes_position_column_names(tmp_path: Path) -> None:
    seed_file = tmp_path / "account_seed_input.csv"
    seed_file.write_text("ACCT000000000001,Acme Trading,ACTIVE\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        f"""
        id: account_csv
        control_data:
          change_type: scd1
        source:
          format: csv
          header: false
          load_method: dbt_seed
          lineterminator: "\\n"
          seed:
            file: {seed_file}
            name: account_seed
            schema: TMP
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
              data_type: varchar(20)
            - id: account_name
              source:
                pos: 1
              data_type: varchar(255)
            - id: account_status
              source:
                pos: 2
              data_type: varchar(20)
        """,
    )

    assert result.errors == []
    assert (output_dir / "seeds" / "account_seed.csv").read_text(encoding="utf-8") == (
        "COL_0,COL_1,COL_2\n"
        "ACCT000000000001,Acme Trading,ACTIVE\n"
    )
    project = load_yaml(output_dir / "dbt_project.yml")
    seed_config = project["seeds"]["type_materialisation_generated"]["account_seed"]
    assert seed_config["+column_types"] == {"COL_0": "varchar", "COL_1": "varchar", "COL_2": "varchar"}
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "cast(COL_0 as string) as COL_0" in source_sql
    assert "cast(COL_2 as string) as COL_2" in source_sql


def test_csv_dbt_seed_generation_outputs_fixed_values_without_seed_columns(tmp_path: Path) -> None:
    seed_file = tmp_path / "account_seed_input.csv"
    seed_file.write_text("account_id\nACCT000000000001\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        f"""
        id: account_csv
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          load_method: dbt_seed
          seed:
            file: {seed_file}
            name: account_seed
            schema: TMP
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                column: account_id
              data_type: varchar(20)
            - id: is_current_flag
              source:
                fixed_value: "Y"
              data_type: varchar(1)
        """,
    )

    assert result.errors == []
    project = load_yaml(output_dir / "dbt_project.yml")
    seed_config = project["seeds"]["type_materialisation_generated"]["account_seed"]
    assert seed_config["+column_types"] == {"ACCOUNT_ID": "varchar"}
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "cast(ACCOUNT_ID as string) as ACCOUNT_ID" in source_sql
    assert "cast('Y' as string) as IS_CURRENT_FLAG" in source_sql


def test_csv_dbt_seed_generation_reports_missing_seed_file(tmp_path: Path) -> None:
    result, _ = generate(
        tmp_path,
        """
        id: account_csv
        control_data:
          change_type: scd1
        source:
          format: csv
          header: true
          load_method: dbt_seed
          seed:
            file: missing.csv
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

    assert len(result.errors) == 1
    assert result.errors[0].location == "$.source.seed.file"
    assert "seed CSV file does not exist" in result.errors[0].message


def test_generated_source_model_uses_csv_positions(tmp_path: Path) -> None:
    # Snowflake staged CSV columns are positional, so pos 2 maps to $3.
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_status
          source:
            pos: 2
            column: account_status
          data_type: varchar(20)
            """
        ),
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "$1::string as ACCOUNT_ID" in source_sql
    assert "$3::string as ACCOUNT_STATUS" in source_sql


def test_generated_source_model_uses_fixed_values(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: is_current_flag
          source:
            fixed_value: "Y"
          data_type: varchar(1)
            """
        ),
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "$1::string as ACCOUNT_ID" in source_sql
    assert "cast('Y' as string) as IS_CURRENT_FLAG" in source_sql
    assert "cast(IS_CURRENT_FLAG as varchar(1)) as IS_CURRENT_FLAG" in model_sql


def test_generated_source_model_uses_default_values(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
            default_value: "UNKNOWN"
          data_type: varchar(20)
            """
        ),
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "coalesce(nullif($1::string, ''), cast('UNKNOWN' as string)) as ACCOUNT_ID" in source_sql


def test_headerless_csv_stage_generation_aliases_positions_to_col_names(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: account_csv
        control_data:
          change_type: scd1
          failure_mode: quarantine_row
        source:
          format: csv
          header: false
          location:
            schema: TMP
            stage: "@csv_stage"
            filename: account.csv
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
              data_type: varchar(20)
              transforms:
                - type: trim
              nullable: false
            - id: account_status
              source:
                pos: 2
              data_type: varchar(20)
        """,
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    quarantine_sql = (output_dir / "models" / "generated" / "account__quarantine.sql").read_text(encoding="utf-8")
    assert "$1::string as COL_0" in source_sql
    assert "$3::string as COL_2" in source_sql
    assert "cast(trim(COL_0) as varchar(20)) as ACCOUNT_ID" in model_sql
    assert "COL_0" in quarantine_sql
    assert "COL_2" in quarantine_sql


def test_generated_final_model_uses_audit_metadata_types(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec())

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "cast('{{ var(\"audit_data_process_key\", \"manual\") }}' as varchar(64))" in model_sql
    assert "cast(current_timestamp() as timestamp_tz) as AUDIT_CREATED_DATETIME" in model_sql
    assert "cast(current_timestamp() as timestamp_tz) as AUDIT_LAST_CHANGED_DATETIME" in model_sql


def test_generated_project_uses_named_local_user_profile(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: account_csv
        control_data:
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
          database: analytics
          schema: business
          fields:
            - id: account_id
              source:
                pos: 0
                column: account_id
              data_type: varchar(20)
        """,
    )

    assert result.errors == []
    project = load_yaml(output_dir / "dbt_project.yml")
    assert project["profile"] == "datahub_type_materialisation"
    assert not (output_dir / "profiles.yml").exists()
    assert output_dir / "profiles.yml" not in result.files


def test_fail_load_generates_validation_guard_model(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          nullable: false
            """
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    guard_sql = (output_dir / "models" / "generated" / "account__validation_guard.sql").read_text(encoding="utf-8")
    not_implemented = (output_dir / "NOT_IMPLEMENTED.md").read_text(encoding="utf-8")
    assert "ref('account__validation_guard')" in model_sql
    assert "TYPE_MATERIALISATION_VALIDATION_FAILED:" in model_sql
    assert "First failure:" in model_sql
    assert "schema=var('tms_staging_schema', 'INTERMEDIATE') | upper" in guard_sql
    assert "pre_hook='drop table if exists {{ this }}'" in guard_sql
    assert "validation_rows as (" in guard_sql
    assert "field `account_id` is null but not nullable" in guard_sql
    assert "VALIDATION_FAILURE_COUNT" in guard_sql
    assert "VALIDATION_FAILURE_DETAILS" in guard_sql
    assert "where FAILURE_DETAILS is not null" in guard_sql
    assert "fail_load validation failure enforcement" not in not_implemented


def test_unique_fields_generate_dbt_validation_sql(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
            """
        ),
    )

    assert result.errors == []
    guard_sql = (output_dir / "models" / "generated" / "account__validation_guard.sql").read_text(encoding="utf-8")
    not_implemented = (output_dir / "NOT_IMPLEMENTED.md").read_text(encoding="utf-8")
    assert "count(*) over (partition by try_cast(cast(ACCOUNT_ID as varchar) as varchar(20))) > 1" in guard_sql
    assert "field `account_id` duplicates a value for a unique field" in guard_sql
    assert "uniqueness checks in generated dbt SQL" not in not_implemented


def test_numeric_transform_validation_casts_via_varchar_for_snowflake(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: rounded_amount
          source:
            pos: 0
            column: rounded_amount
          data_type: number(10,2)
          transforms:
            - type: round
              scale: 2
            """
        ),
    )

    assert result.errors == []
    guard_sql = (output_dir / "models" / "generated" / "account__validation_guard.sql").read_text(encoding="utf-8")
    assert "try_cast(cast(round(ROUNDED_AMOUNT, 2) as varchar) as number(10,2)) is null" in guard_sql


def test_parse_date_and_timestamp_transforms_generate_snowflake_sql(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              failure_mode: fail_load
            """,
            fields="""
        - id: opened_on
          source:
            pos: 0
            column: opened_on
          data_type: date
          transforms:
            - type: parse_date
              format: "%d/%m/%Y"
        - id: opened_at
          source:
            pos: 1
            column: opened_at
          data_type: timestamp_tz
          transforms:
            - type: parse_timestamp
              format: "%Y-%m-%dT%H:%M:%S%z"
        - id: reviewed_at
          source:
            pos: 2
            column: reviewed_at
          data_type: timestamp_tz
          transforms:
            - type: parse_timestamp
              format: "%Y-%m-%d %H:%M:%S"
              timezone_if_missing: UTC
        - id: valid_to_datetime
          source:
            pos: 3
            column: VALID_TO_DATETIME
          data_type: timestamp_tz
          transforms:
            - type: parse_timestamp
              format: "%d/%m/%Y"
              timezone_if_missing: UTC
              time_if_missing: end_of_day
            """
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    guard_sql = (output_dir / "models" / "generated" / "account__validation_guard.sql").read_text(encoding="utf-8")
    assert "try_to_date(cast(OPENED_ON as varchar), 'DD/MM/YYYY')" in model_sql
    assert "try_to_timestamp_tz(cast(OPENED_AT as varchar), 'YYYY-MM-DD\"T\"HH24:MI:SSTZHTZM')" in model_sql
    assert "try_to_timestamp_tz(concat(cast(REVIEWED_AT as varchar), ' +0000'), 'YYYY-MM-DD HH24:MI:SS TZHTZM')" in model_sql
    assert (
        "dateadd(nanosecond, -1, dateadd(day, 1, date_trunc('day', "
        "try_to_timestamp_tz(concat(cast(VALID_TO_DATETIME as varchar), ' +0000'), 'DD/MM/YYYY TZHTZM'))))"
        in model_sql
    )
    assert "field `opened_on` does not match parse_date format `%d/%m/%Y`" in guard_sql
    assert "field `opened_at` does not match parse_timestamp format `%Y-%m-%dT%H:%M:%S%z`" in guard_sql


def test_parse_timestamp_generation_rejects_missing_timezone_policy(tmp_path: Path) -> None:
    result, _ = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: opened_at
          source:
            pos: 0
            column: opened_at
          data_type: timestamp_tz
          transforms:
            - type: parse_timestamp
              format: "%Y-%m-%d %H:%M:%S"
            """
        ),
    )

    assert diagnostic_messages(result.errors) == [
        "`parse_timestamp` dbt generation requires a timezone directive in `format` or `timezone_if_missing`"
    ]


def test_table_source_generation_reads_from_configured_relation(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          database: raw
          schema: landing
          table: account_source
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

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "source_query.ACCOUNT_ID as ACCOUNT_ID" in source_sql
    assert "select * from RAW.LANDING.ACCOUNT_SOURCE" in source_sql
    assert "from source_query" in source_sql


def test_table_source_generation_wraps_source_query(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          query: |
            with latest_source as (
              select account_id, load_batch_id
              from landing.account_source
              where load_batch_id = '{{ var('load_batch_id') }}'
            )
            select account_id
            from latest_source
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

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "with source_query as (" in source_sql
    assert "    with latest_source as (" in source_sql
    assert "      from landing.account_source" in source_sql
    assert "      where load_batch_id = '{{ var('load_batch_id') }}'" in source_sql
    assert "from source_query" in source_sql


def test_table_source_generation_extracts_snowflake_paths_as_varchar(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          database: raw
          schema: landing
          table: account_events
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                column: payload
                snowflake_path: account.id
              data_type: varchar(20)
            - id: opened_on
              source:
                column: payload
                snowflake_path: account.openedDate
              data_type: date
              transforms:
                - type: parse_date
                  format: "%Y-%m-%d"
        """,
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    final_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "to_varchar(get_path(source_query.PAYLOAD, 'account.id')) as ACCOUNT_ID" in source_sql
    assert "to_varchar(get_path(source_query.PAYLOAD, 'account.openedDate')) as OPENED_ON" in source_sql
    assert "from RAW.information_schema.columns" in source_sql
    assert "and upper(column_name) in ('PAYLOAD')" in source_sql
    assert "data_type not in ('VARIANT', 'OBJECT', 'ARRAY')" in source_sql
    assert "try_to_date(cast(OPENED_ON as varchar), 'YYYY-MM-DD')" in final_sql


def test_table_source_generation_uses_fixed_values(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: landing
          table: account_events
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                column: account_id
              data_type: varchar(20)
            - id: is_current_flag
              source:
                fixed_value: "Y"
              data_type: varchar(1)
        """,
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "source_query.ACCOUNT_ID as ACCOUNT_ID" in source_sql
    assert "cast('Y' as string) as IS_CURRENT_FLAG" in source_sql
    assert "from source_query" in source_sql


def test_table_source_generation_uses_default_values(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: landing
          table: account_events
        target:
          id: account
          schema: business
          fields:
            - id: account_id
              source:
                column: account_id
                default_value: "UNKNOWN"
              data_type: varchar(20)
        """,
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "coalesce(nullif(to_varchar(source_query.ACCOUNT_ID), ''), cast('UNKNOWN' as string)) as ACCOUNT_ID" in source_sql


def test_table_source_generation_uses_standalone_default_values(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: landing
          table: account_events
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

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "cast('AMID' as string) as EXTERNAL_IDENTIFICATION_TYPE" in source_sql


def test_table_source_generation_uses_default_from_field(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
          business_key:
            fields:
              - customer_id
        source:
          format: table
          schema: landing
          table: customer_events
        target:
          id: customer
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

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "customer__source.sql").read_text(encoding="utf-8")
    assert "to_varchar(source_query.CUSTOMER_ID) as CLV_ID" in source_sql


def test_table_source_generation_uses_source_macro(tmp_path: Path) -> None:
    macro_dir = tmp_path / "macros"
    macro_dir.mkdir()
    (macro_dir / "lookup_macros.py").write_text(
        """
class CustomerStatusKey:
    def generate_dbt_macro(self):
        return "{% macro customer_status_key(reference_type, source_system, source_code_expression, ref_alias, output_column) %}left join lateral (select '{{ reference_type }}:' || '{{ source_system }}:' || ({{ source_code_expression }}) as {{ output_column }}) as {{ ref_alias }} on true{% endmacro %}"

customer_status_key = CustomerStatusKey()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          query: |
            select
              customer_id,
              status_code
            from raw.customer
        target:
          id: customer
          schema: business
          fields:
            - id: customer_status_key
              source:
                macro: lookup_macros.customer_status_key
                args:
                  reference_type: CUSTOMER_STATUS
                  source_system: V10
                  source_code_expression: source_query.STATUS_CODE
              data_type: varchar(64)
            - id: customer_id
              source:
                column: customer_id
              data_type: varchar(20)
        """,
        macro_paths=[macro_dir],
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "customer__source.sql").read_text(encoding="utf-8")
    assert "materialized='view'" in source_sql
    assert 'LOOKUP_CUSTOMER_STATUS_KEY."CUSTOMER_STATUS_KEY" as CUSTOMER_STATUS_KEY' in source_sql
    assert '{{ customer_status_key(output_column="customer_status_key", ref_alias="LOOKUP_CUSTOMER_STATUS_KEY", reference_type="CUSTOMER_STATUS", source_code_expression="source_query.STATUS_CODE", source_system="V10") }}' in source_sql
    macro_sql = (output_dir / "macros" / "generated" / "customer_status_key.sql").read_text(encoding="utf-8")
    assert "{% macro customer_status_key(" in macro_sql


def test_table_source_generation_flattens_snowflake_paths(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
        control_data:
          change_type: scd1
        source:
          format: table
          schema: landing
          table: order_events
          flatten:
            column: payload
            path: customer.orders
            alias: order_item
            mode: array
        target:
          id: order
          schema: business
          fields:
            - id: account_id
              source:
                column: payload
                snowflake_path: customer.account.id
              data_type: varchar(20)
            - id: order_id
              source:
                column: order_item
                snowflake_path: id
              data_type: varchar(20)
        """,
    )

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "order__source.sql").read_text(encoding="utf-8")
    assert "to_varchar(get_path(source_query.PAYLOAD, 'customer.account.id')) as ACCOUNT_ID" in source_sql
    assert "to_varchar(get_path(ORDER_ITEM.value, 'id')) as ORDER_ID" in source_sql
    assert ", lateral flatten(input => get_path(source_query.PAYLOAD, 'customer.orders'), mode => 'ARRAY') as ORDER_ITEM" in source_sql


def test_job_event_hooks_are_generated_at_project_run_level(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec())

    assert result.errors == []
    project = load_yaml(output_dir / "dbt_project.yml")
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert len(project["on-run-start"]) == 2
    assert "var('tms_enable_job_hooks', true)" in project["on-run-start"][0]
    assert "create table if not exists {{ var('tms_job_schema', 'BUSINESS') | upper }}.TYPE_MATERIALISATION_JOBS" in project["on-run-start"][0]
    assert "EVENT_TIMESTAMP timestamp_tz" in project["on-run-start"][0]
    assert "DETAILS varchar(16777216)" in project["on-run-start"][0]
    assert "SPEC_FILE_NAME varchar(1024)" in project["on-run-start"][0]
    assert "GENERATED_TABLE varchar(1024)" in project["on-run-start"][0]
    assert "QUARANTINE_TABLE varchar(1024)" in project["on-run-start"][0]
    assert "LOADED_COUNT number(38, 0)" in project["on-run-start"][0]
    assert "QUARANTINE_COUNT number(38, 0)" in project["on-run-start"][0]
    assert "'JOB_START'" in project["on-run-start"][1]
    assert "cast('{{ invocation_id }}' as varchar(64))" in project["on-run-start"][1]
    assert "cast(current_timestamp() as timestamp_tz)" in project["on-run-start"][1]
    assert 'var("job_id"' not in project["on-run-start"][1]
    assert "DETAILS, SPEC_FILE_NAME, GENERATED_TABLE, QUARANTINE_TABLE, LOADED_COUNT, QUARANTINE_COUNT" in project["on-run-start"][1]
    assert "cast('spec.yaml' as varchar(1024))" in project["on-run-start"][1]
    assert "cast('{{ target.database | upper }}.{{ var(\"target_schema\", \"BUSINESS\") | upper }}.ACCOUNT' as varchar(1024))" in project["on-run-start"][1]
    assert "cast(null as varchar(1024))" in project["on-run-start"][1]
    assert "cast(null as number(38, 0))" in project["on-run-start"][1]
    assert len(project["on-run-end"]) == 2
    assert "create table if not exists {{ var('tms_job_schema', 'BUSINESS') | upper }}.TYPE_MATERIALISATION_JOBS" in project["on-run-end"][0]
    assert "var('tms_enable_job_hooks', true)" in project["on-run-end"][1]
    assert "'JOB_END'" in project["on-run-end"][1]
    assert "cast('{{ invocation_id }}' as varchar(64))" in project["on-run-end"][1]
    assert "cast(current_timestamp() as timestamp_tz)" in project["on-run-end"][1]
    assert 'var("job_id"' not in project["on-run-end"][1]
    assert 'var("job_result", "COMPLETED")' not in project["on-run-end"][1]
    assert 'var("job_details", none)' in project["on-run-end"][1]
    assert "validation_guard_failed.value" in project["on-run-end"][1]
    assert "'TYPE_MATERIALISATION_VALIDATION_FAILED' in (result.message | string)" in project["on-run-end"][1]
    assert "'validation errors failed the load'" in project["on-run-end"][1]
    assert "'dbt run failed; inspect dbt artifacts for runtime details'" in project["on-run-end"][1]
    assert "case when quarantine_counts.QUARANTINE_COUNT > 0 then 'COMPLETED_WITH_QUARANTINE' else 'COMPLETED' end" in project["on-run-end"][1]
    assert "'COMPLETED_WITH_QUARANTINE'" in project["on-run-end"][1]
    assert "case when quarantine_counts.QUARANTINE_COUNT > 0 then 'validation errors written to quarantine output' else null end" in project["on-run-end"][1]
    assert 'adapter.get_relation(database=(target.database | upper), schema=(var("target_schema", "BUSINESS") | upper), identifier=\'ACCOUNT\')' in project["on-run-end"][1]
    assert "loaded_counts as (select {% if tms_generated_relation is not none %}(select count(*) from {{ tms_generated_relation }}){% else %}null{% endif %} as LOADED_COUNT)" in project["on-run-end"][1]
    assert "{% set tms_quarantine_relation = none %}" in project["on-run-end"][1]
    assert "quarantine_counts as (select {% if tms_quarantine_relation is not none %}(select count(*) from {{ tms_quarantine_relation }}){% else %}null{% endif %} as QUARANTINE_COUNT)" in project["on-run-end"][1]
    assert "loaded_counts.LOADED_COUNT" in project["on-run-end"][1]
    assert "quarantine_counts.QUARANTINE_COUNT" in project["on-run-end"][1]
    assert "from loaded_counts cross join quarantine_counts" in project["on-run-end"][1]
    assert "results | selectattr('status', 'equalto', 'error')" in project["on-run-end"][1]
    assert "results | selectattr('status', 'equalto', 'fail')" in project["on-run-end"][1]
    assert "'FAILED'" in project["on-run-end"][1]
    assert "pre_hook" not in model_sql
    assert "post_hook" not in model_sql


def test_generated_schema_name_macro_supports_runtime_schema_override(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec())

    assert result.errors == []
    macro_sql = (output_dir / "macros" / "generated" / "generate_schema_name.sql").read_text(encoding="utf-8")
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "schema=var('target_schema', 'BUSINESS') | upper" in model_sql
    assert "{{ custom_schema_name | trim | upper }}" in macro_sql


def test_generated_project_never_creates_schemas(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec())

    assert result.errors == []
    macro_sql = (output_dir / "macros" / "generated" / "create_schema.sql").read_text(encoding="utf-8")
    assert "{% macro create_schema(relation) -%}" in macro_sql
    assert "create schema" not in macro_sql.lower()


def test_job_event_hooks_include_quarantine_relation_when_enabled(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              failure_mode: quarantine_row
            """
        ),
    )

    assert result.errors == []
    project = load_yaml(output_dir / "dbt_project.yml")
    expected_quarantine = "cast('{{ target.database | upper }}.{{ var(\"tms_staging_schema\", \"INTERMEDIATE\") | upper }}.ACCOUNT__QUARANTINE' as varchar(1024))"
    assert expected_quarantine in project["on-run-start"][1]
    assert expected_quarantine in project["on-run-end"][1]
    assert 'adapter.get_relation(database=(target.database | upper), schema=(var("tms_staging_schema", "INTERMEDIATE") | upper), identifier=\'ACCOUNT__QUARANTINE\')' in project["on-run-end"][1]


def test_quarantine_model_is_incremental_and_append_only(tmp_path: Path) -> None:
    # The quarantine relation may expand for new source fields, but must not shrink existing columns.
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              failure_mode: quarantine_row
            """
        ),
    )

    assert result.errors == []
    quarantine_sql = (output_dir / "models" / "generated" / "account__quarantine.sql").read_text(encoding="utf-8")
    assert "materialized='incremental'" in quarantine_sql
    assert "schema=var('tms_staging_schema', 'INTERMEDIATE') | upper" in quarantine_sql
    assert "incremental_strategy='append'" in quarantine_sql
    assert "on_schema_change='append_new_columns'" in quarantine_sql
    assert "alias='ACCOUNT__QUARANTINE'" in quarantine_sql
    assert "cast('{{ invocation_id }}' as varchar(64)) as JOB_ID" in quarantine_sql
    assert 'var("job_id"' not in quarantine_sql
    assert "FAILURE_DETAILS" in quarantine_sql


def test_quarantine_model_uses_generated_uniqueness_validation(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              failure_mode: quarantine_row
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
            """,
        ),
    )

    assert result.errors == []
    quarantine_sql = (output_dir / "models" / "generated" / "account__quarantine.sql").read_text(encoding="utf-8")
    assert "count(*) over (partition by try_cast(cast(ACCOUNT_ID as varchar) as varchar(20))) > 1" in quarantine_sql
    assert "from validation_rows" in quarantine_sql


def test_quarantine_config_defaults_can_be_overridden(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              failure_mode: quarantine_row
              quarantine:
                database: ops
                schema: data_quality
                table: account_bad_rows
            """
        ),
    )

    assert result.errors == []
    quarantine_sql = (output_dir / "models" / "generated" / "account__quarantine.sql").read_text(encoding="utf-8")
    assert "database='OPS'" in quarantine_sql
    assert "schema='DATA_QUALITY'" in quarantine_sql
    assert "alias='ACCOUNT_BAD_ROWS'" in quarantine_sql


def test_generated_unit_tests_include_final_and_quarantine_models(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id\nACCT000000000001\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              failure_mode: quarantine_row
            """
        ),
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    assert [unit_test["model"] for unit_test in unit_test_yaml["unit_tests"]] == ["account", "account__quarantine"]
    assert unit_test_yaml["unit_tests"][0]["given"][0]["input"] == "ref('account__source')"
    assert unit_test_yaml["unit_tests"][0]["given"][0]["format"] == "sql"
    assert "cast('ACCT000000000001' as varchar) as ACCOUNT_ID" in unit_test_yaml["unit_tests"][0]["given"][0]["rows"]
    assert unit_test_yaml["unit_tests"][0]["overrides"]["vars"] == {"tms_unit_test": True}
    assert unit_test_yaml["unit_tests"][1]["given"][0]["format"] == "sql"
    assert unit_test_yaml["unit_tests"][1]["expect"]["rows"] == []
    assert unit_test_yaml["unit_tests"][1]["overrides"]["vars"] == {"tms_unit_test": True}


def test_scd2_auto_generation_adds_business_data_hash(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    not_implemented = (output_dir / "NOT_IMPLEMENTED.md").read_text(encoding="utf-8")
    assert "cast(sha2(concat_ws('|', coalesce(cast(cast(ACCOUNT_ID as varchar(20)) as varchar), '')), 256) as varchar(64)) as BUSINESS_DATA_HASH" in model_sql
    assert "SCD2 duplicate-hash historical boundary handling" not in not_implemented


def test_business_key_generation_uses_fixed_sha2_pipe_hash(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              business_key:
                fields:
                  - account_id
                  - account_name
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert (
        "cast(sha2(concat_ws('|', coalesce(cast(cast(ACCOUNT_ID as varchar(20)) as varchar), ''), "
        "coalesce(cast(cast(ACCOUNT_NAME as varchar(255)) as varchar), '')), 256) as varchar) "
        "as ACCOUNT_BUSINESS_KEY"
        in model_sql
    )


def test_surrogate_key_generation_defaults_to_id_key(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "cast(uuid_string() as varchar(36)) as ACCOUNT_KEY" in model_sql


def test_business_key_generation_can_be_disabled(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              skip_business_key: true
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "ACCOUNT_BUSINESS_KEY" not in model_sql


def test_scd1_generation_adds_business_data_hash_by_default(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert (
        "cast(sha2(concat_ws('|', coalesce(cast(cast(ACCOUNT_ID as varchar(20)) as varchar), ''), "
        "coalesce(cast(cast(ACCOUNT_NAME as varchar(255)) as varchar), '')), 256) as varchar(64)) "
        "as BUSINESS_DATA_HASH"
        in model_sql
    )
    assert "cast(cast(ACCOUNT_KEY" not in model_sql
    assert "cast(cast(ACCOUNT_BUSINESS_KEY" not in model_sql


def test_business_data_hash_generation_can_be_disabled(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              business_data_hash:
                skip_business_data_hash: true
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "BUSINESS_DATA_HASH" not in model_sql


def test_business_data_hash_include_uses_configured_fields(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              business_data_hash:
                business_data_hash_mode: include
                fields:
                  - account_name
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
          transforms:
            - type: trim
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    hash_line = next(line for line in model_sql.splitlines() if " as BUSINESS_DATA_HASH" in line)
    assert "trim(ACCOUNT_NAME)" in hash_line
    assert "varchar(255)" in hash_line
    assert "ACCOUNT_ID" not in hash_line


def test_surrogate_key_generation_can_be_disabled(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              skip_surrogate_key: true
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "uuid_string()" not in model_sql
    assert "ACCOUNT_KEY" not in model_sql


def test_scd2_auto_generation_preserves_existing_surrogate_keys(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "cast(uuid_string() as varchar(36)) as ACCOUNT_KEY" in model_sql
    assert "existing_target.ACCOUNT_KEY as ACCOUNT_KEY" in model_sql
    assert "ACCOUNT_KEY" in model_sql
    assert (
        "cast(sha2(concat_ws('|', coalesce(cast(cast(ACCOUNT_ID as varchar(20)) as varchar), ''), "
        "coalesce(cast(cast(ACCOUNT_NAME as varchar(255)) as varchar), '')), 256) as varchar(64)) "
        "as BUSINESS_DATA_HASH"
        in model_sql
    )
    assert "cast(cast(ACCOUNT_KEY" not in model_sql


def test_scd2_auto_generation_adds_continuous_validity_windows(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "cast('{{ var('insert_time') }}' as timestamp_tz) as TMS_VALID_FROM_DATETIME_CANDIDATE" in model_sql
    assert "row_number() over (partition by ACCOUNT_BUSINESS_KEY order by TMS_VALID_FROM_DATETIME_CANDIDATE) = 1" in model_sql
    assert "cast('0001-01-01T00:00:00Z' as timestamp_tz)" in model_sql
    assert "lead(VALID_FROM_DATETIME) over (partition by ACCOUNT_BUSINESS_KEY order by VALID_FROM_DATETIME)" in model_sql
    assert (
        "dateadd(nanosecond, -1, lead(VALID_FROM_DATETIME) over "
        "(partition by ACCOUNT_BUSINESS_KEY order by VALID_FROM_DATETIME))"
        in model_sql
    )
    assert "cast('9999-12-31T23:59:59Z' as timestamp_tz)" in model_sql
    assert "end as IS_CURRENT_FLAG" in model_sql
    assert "'N' as TMS_IS_DELETED_FLAG_CANDIDATE" in model_sql
    assert "BUSINESS_DATA_HASH" in model_sql
    assert "post_load_validation_rows as (" in model_sql
    assert "from post_load_validation_rows" in model_sql
    assert "scd2_invalid_validity_rows as (" in model_sql
    assert "(VALID_TO_DATETIME <= VALID_FROM_DATETIME)" in model_sql
    assert "(TMS_NEXT_VALID_FROM_DATETIME <= VALID_TO_DATETIME)" in model_sql
    assert (
        "(TMS_NEXT_VALID_FROM_DATETIME is not null "
        "and TMS_NEXT_VALID_FROM_DATETIME <> dateadd(nanosecond, 1, VALID_TO_DATETIME))"
        in model_sql
    )
    assert (
        "-- Accept near-end-of-time values so timezone normalisation of "
        "9999-12-31 timestamps does not falsely reject open-ended rows."
        in model_sql
    )
    assert (
        "(TMS_NEXT_VALID_FROM_DATETIME is null "
        "and VALID_TO_DATETIME < cast('9999-12-30 00:00:00' as timestamp_tz))"
        in model_sql
    )
    assert (
        "cast(concat('TYPE_MATERIALISATION_SCD2_VALIDATION_FAILED:', SCD2_VALIDATION_FAILURE_COUNT) as number)"
        in model_sql
    )
    assert "cross join scd2_validation_guard" in model_sql
    assert "where scd2_validation_guard.SCD2_VALIDATION_GUARD = 0" in model_sql


def test_scd2_auto_sparse_validation_allows_gaps(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
                scd2_validation: sparse
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "scd2_invalid_validity_rows as (" in model_sql
    assert "(VALID_TO_DATETIME <= VALID_FROM_DATETIME)" in model_sql
    assert "(TMS_NEXT_VALID_FROM_DATETIME <= VALID_TO_DATETIME)" in model_sql
    assert "dateadd(nanosecond, 1, VALID_TO_DATETIME)" not in model_sql
    assert "TMS_NEXT_VALID_FROM_DATETIME is null and VALID_TO_DATETIME <> cast" not in model_sql


def test_scd2_auto_from_sot_false_uses_insert_time_for_first_version(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
                scd2_auto_from_sot: false
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "TMS_VALID_FROM_DATETIME_CANDIDATE as VALID_FROM_DATETIME" in model_sql
    assert "then cast('0001-01-01T00:00:00Z' as timestamp_tz)" not in model_sql


def test_scd2_manual_generation_copies_declared_scd_fields(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_manual
              business_key:
                fields:
                  - account_id
              scd:
                update_mode: upsert
                update_key:
                  fields:
                    - valid_from_datetime
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
        - id: valid_from_datetime
          source:
            pos: 2
            column: VALID_FROM_DATETIME
          data_type: timestamp_tz
        - id: valid_to_datetime
          source:
            pos: 3
            column: VALID_TO_DATETIME
          data_type: timestamp_tz
        - id: is_current_flag
          source:
            pos: 4
            column: IS_CURRENT_FLAG
          data_type: varchar(1)
        - id: is_deleted_flag
          source:
            pos: 5
            column: IS_DELETED_FLAG
          data_type: varchar(1)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "materialized='incremental'" in model_sql
    assert "incremental_strategy='delete+insert'" in model_sql
    assert "unique_key=['ACCOUNT_BUSINESS_KEY', 'VALID_FROM_DATETIME']" in model_sql
    assert "cast(VALID_FROM_DATETIME as timestamp_tz) as VALID_FROM_DATETIME" in model_sql
    assert "cast(VALID_TO_DATETIME as timestamp_tz) as VALID_TO_DATETIME" in model_sql
    assert "cast(IS_CURRENT_FLAG as varchar(1)) as IS_CURRENT_FLAG" in model_sql
    assert "cast(IS_DELETED_FLAG as varchar(1)) as IS_DELETED_FLAG" in model_sql
    hash_line = next(line for line in model_sql.splitlines() if " as BUSINESS_DATA_HASH" in line)
    assert "ACCOUNT_ID" in hash_line
    assert "ACCOUNT_NAME" in hash_line
    assert "VALID_FROM_DATETIME" not in hash_line
    assert "VALID_TO_DATETIME" not in hash_line
    assert "IS_CURRENT_FLAG" not in hash_line
    assert "IS_DELETED_FLAG" not in hash_line


def test_scd2_manual_generation_rejects_multiple_current_rows_for_entity_key(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_manual
              business_key:
                fields:
                  - account_id
              scd:
                update_mode: upsert
                update_key:
                  fields:
                    - valid_from_datetime
            """,
            fields="""
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
        ),
    )

    assert result.errors == []
    guard_sql = (output_dir / "models" / "generated" / "account__validation_guard.sql").read_text(encoding="utf-8")
    assert "scd2_manual has multiple current rows for the same current-row key" in guard_sql
    assert "adapter.get_relation(database=(target.database | upper), schema=(var(\"target_schema\", \"BUSINESS\") | upper), identifier='ACCOUNT')" in guard_sql
    assert "existing_manual_rows as (" in guard_sql
    assert "remaining_existing_manual_rows as (" in guard_sql
    assert "manual_candidate_rows as (" in guard_sql
    assert "count_if(IS_CURRENT_FLAG = 'Y') over (partition by ACCOUNT_BUSINESS_KEY)" in guard_sql
    assert "where ((incoming_manual_rows.ACCOUNT_BUSINESS_KEY = existing_target.ACCOUNT_BUSINESS_KEY)" in guard_sql
    assert "incoming_manual_rows.VALID_FROM_DATETIME = existing_manual_rows.VALID_FROM_DATETIME" in guard_sql


def test_scd2_manual_generation_rejects_live_target_validity_window_overlap(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_manual
              business_key:
                fields:
                  - account_id
              scd:
                update_mode: upsert
            """,
            fields="""
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
        ),
    )

    assert result.errors == []
    guard_sql = (output_dir / "models" / "generated" / "account__validation_guard.sql").read_text(encoding="utf-8")
    assert "lead(VALID_FROM_DATETIME) over (partition by ACCOUNT_BUSINESS_KEY order by VALID_FROM_DATETIME)" in guard_sql
    assert "TMS_NEXT_VALID_FROM_DATETIME <= VALID_TO_DATETIME" in guard_sql
    assert "scd2_manual validity windows overlap for the same business key" in guard_sql
    assert "manual_state_failures as (" in guard_sql


def test_scd2_manual_quarantine_uses_live_target_validity_window_validation(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_manual
              failure_mode: quarantine_row
              business_key:
                fields:
                  - account_id
              scd:
                update_mode: upsert
            """,
            fields="""
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
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    quarantine_sql = (output_dir / "models" / "generated" / "account__quarantine.sql").read_text(encoding="utf-8")
    assert "from valid_rows" in model_sql
    assert "where FAILURE_DETAILS is null" in model_sql
    assert "existing_manual_rows as (" in quarantine_sql
    assert "manual_candidate_rows as (" in quarantine_sql
    assert "scd2_manual validity windows overlap for the same business key" in quarantine_sql


def test_scd2_manual_upsert_defaults_update_key_to_valid_from_datetime(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_manual
              business_key:
                fields:
                  - account_id
              scd:
                update_mode: upsert
            """,
            fields="""
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
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "unique_key=['ACCOUNT_BUSINESS_KEY', 'VALID_FROM_DATETIME']" in model_sql


def test_scd2_manual_defaults_to_append_only_incremental_mode(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_manual
            """,
            fields="""
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
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "materialized='incremental'" in model_sql
    assert "incremental_strategy='append'" in model_sql
    assert "unique_key=" not in model_sql


def test_scd2_manual_truncate_before_load_uses_table_materialization(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_manual
              truncate_before_load: true
            """,
            fields="""
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
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "materialized='table'" in model_sql
    assert "incremental_strategy=" not in model_sql
    assert "allow_truncate" in model_sql


def test_scd2_auto_generation_rejects_missing_insert_time(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
            """,
        ),
    )
    del output_dir

    assert diagnostic_messages(result.errors) == ["`scd.insert_time` is required when `change_type` is scd2_auto"]
    assert result.errors[0].location == "$.control_data.scd.insert_time"


def test_scd1_generation_hard_deletes_field_marked_rows(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              scd:
                delete_detection:
                  mode: field
                  field: account_status
                  value: DELETED
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_status
          source:
            pos: 1
            column: account_status
          data_type: varchar(20)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "where not (ACCOUNT_STATUS = 'DELETED')" in model_sql


def test_truncate_before_load_requires_allow_truncate_var(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd1
              truncate_before_load: true
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "materialized='table'" in model_sql
    assert "allow_truncate" in model_sql
    assert "exceptions.raise_compiler_error" in model_sql
    assert "truncate_before_load requires dbt var `allow_truncate: true`" in model_sql
    assert "where not" not in model_sql


def test_generation_rejects_missing_business_key(tmp_path: Path) -> None:
    result, _ = generate(
        tmp_path,
        """
        id: account_csv
        control_data:
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
        auto_business_key=False,
    )

    assert diagnostic_messages(result.errors) == [
        "`business_key.fields` must contain at least one field id"
    ]
    assert result.errors[0].location == "$.control_data.business_key.fields"


def test_scd2_auto_queries_existing_target(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "materialized='incremental'" in model_sql
    assert "incremental_strategy='delete+insert'" in model_sql
    assert "unique_key=['ACCOUNT_BUSINESS_KEY']" in model_sql
    assert "from {{ this }}" in model_sql
    assert "current_target_rows as (" in model_sql
    assert "where IS_CURRENT_FLAG = 'Y'" in model_sql
    assert "source_change_rows as (" in model_sql
    assert "current_target.BUSINESS_DATA_HASH = typed_rows.BUSINESS_DATA_HASH" in model_sql
    assert "synthetic_delete_rows as (" in model_sql
    assert "where coalesce(current_target.IS_DELETED_FLAG, 'N') <> 'Y'" not in model_sql
    assert "existing_target.VALID_FROM_DATETIME as TMS_VALID_FROM_DATETIME_CANDIDATE" in model_sql


def test_scd2_auto_truncate_before_load_rebuilds_without_existing_target(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              truncate_before_load: true
              scd:
                insert_time: "{{ var('insert_time') }}"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "materialized='table'" in model_sql
    assert "incremental_strategy='delete+insert'" not in model_sql
    assert "from {{ this }}" not in model_sql
    assert "current_target_rows as (" not in model_sql
    assert "allow_truncate" in model_sql
    assert "exceptions.raise_compiler_error" in model_sql
    assert "row_number() over (partition by ACCOUNT_BUSINESS_KEY order by TMS_VALID_FROM_DATETIME_CANDIDATE) = 1" in model_sql
    assert "scd2_invalid_validity_rows as (" in model_sql
    assert "cross join scd2_validation_guard" in model_sql


def test_scd2_auto_generates_duplicate_hash_boundary_updates(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "{{ var('insert_time') }}"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "TMS_PREVIOUS_IS_EXISTING_TARGET_ROW = 'N'" in model_sql
    assert "TMS_PREVIOUS_BUSINESS_DATA_HASH is not null" in model_sql
    assert "and not (TMS_PREVIOUS_2_BUSINESS_DATA_HASH is not null" in model_sql
    assert "SCD2 duplicate hash handling: historical duplicate boundaries updated=" in model_sql
    assert "historical duplicate rows skipped" not in model_sql


def test_scd2_auto_unit_tests_override_is_incremental(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id,account_name\nACCT000000000001,Acme Trading\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "2026-07-05T00:00:00Z"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    assert unit_test_yaml["unit_tests"][0]["overrides"] == {
        "vars": {"tms_unit_test": True},
        "macros": {"is_incremental": False},
    }


def test_scd2_unit_tests_include_business_data_hash_expectation(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id,account_name\nACCT000000000001, Acme Trading \n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "2026-07-05T00:00:00Z"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
          transforms:
            - type: trim
            """,
        ),
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    expected_row = unit_test_yaml["unit_tests"][0]["expect"]["rows"][0]
    assert expected_row["ACCOUNT_NAME"] == "Acme Trading"
    assert expected_row["BUSINESS_DATA_HASH"] == hashlib.sha256(b"ACCT000000000001|Acme Trading").hexdigest()


def test_scd2_unit_tests_include_continuous_validity_windows(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text(
        "\n".join(
            [
                "account_id,account_name",
                "ACCT000000000001,Acme Trading",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "2026-07-05T00:00:00Z"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    expected_rows = unit_test_yaml["unit_tests"][0]["expect"]["rows"]
    assert expected_rows[0]["VALID_FROM_DATETIME"] == "0001-01-01T00:00:00Z"
    assert expected_rows[0]["VALID_TO_DATETIME"] == "9999-12-31T23:59:59Z"
    assert expected_rows[0]["IS_CURRENT_FLAG"] == "Y"
    assert expected_rows[0]["IS_DELETED_FLAG"] == "N"


def test_scd2_unit_tests_use_insert_time_when_from_sot_is_false(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id,account_name\nACCT000000000001,Acme Trading\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "2026-07-05T00:00:00Z"
                scd2_auto_from_sot: false
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    expected_rows = unit_test_yaml["unit_tests"][0]["expect"]["rows"]
    assert expected_rows[0]["VALID_FROM_DATETIME"] == "2026-07-05T00:00:00Z"
    assert expected_rows[0]["VALID_TO_DATETIME"] == "9999-12-31T23:59:59Z"
    assert expected_rows[0]["IS_CURRENT_FLAG"] == "Y"


def test_scd2_unit_tests_reject_insert_time_without_timezone(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id,account_name\nACCT000000000001,Acme Trading\n", encoding="utf-8")
    result, _ = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2_auto
              scd:
                insert_time: "2026-07-01 00:00:00"
            """,
            fields="""
        - id: account_id
          source:
            pos: 0
            column: account_id
          data_type: varchar(20)
          unique: true
        - id: account_name
          source:
            pos: 1
            column: account_name
          data_type: varchar(255)
            """,
        ),
        unit_test_csv=csv_path,
    )

    assert diagnostic_messages(result.errors) == ["unit-test timestamp value must include a timezone"]
    assert result.errors[0].location == str(csv_path)


def test_custom_macro_files_are_generated(tmp_path: Path) -> None:
    # Generation consumes Python macro contracts and writes dbt/Jinja macro files.
    macro_dir = tmp_path / "macros"
    macro_dir.mkdir()
    (macro_dir / "my_macros.py").write_text(
        """
class UpperAccountName:
    def generate_dbt_macro(self):
        return "{% macro upper_account_name(column_expression) %}upper({{ column_expression }}){% endmacro %}"

upper_account_name = UpperAccountName()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            fields="""
        - id: account_name
          source:
            pos: 0
            column: account_name
          data_type: varchar(255)
          transforms:
            - type: custom
              macro: my_macros.upper_account_name
            """
        ),
    )

    assert result.errors == []
    assert [warning.location for warning in result.warnings] == ["my_macros.upper_account_name"]
    macro_sql = (output_dir / "macros" / "generated" / "upper_account_name.sql").read_text(encoding="utf-8")
    assert "{% macro upper_account_name(column_expression) %}" in macro_sql
