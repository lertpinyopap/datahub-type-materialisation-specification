"""Tests for generating a runnable dbt project from a materialisation spec.

These checks cover CSV and table source models, generated model SQL, job hooks,
quarantine models, generated unit tests, and unsupported feature reporting.
"""

import hashlib
from pathlib import Path
from textwrap import indent

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
    control_block = indent(extra_control.strip(), "    ") if extra_control.strip() else ""
    if not control_block:
        control_block = "    control_data:\n      change_type: scd1"
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


def generate(tmp_path: Path, spec_content: str, **options):
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
            file: {seed_file.name}
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
            file: {seed_file.name}
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
    assert "validation_rows as (" in guard_sql
    assert "field `account_id` is null but not nullable" in guard_sql
    assert "cast('TYPE_MATERIALISATION_VALIDATION_FAILED' as number)" in guard_sql
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
    assert "ACCOUNT_ID as ACCOUNT_ID" in source_sql
    assert "from RAW.LANDING.ACCOUNT_SOURCE" in source_sql


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
    assert "var('target_schema', none)" in macro_sql
    assert "{{ override_schema | trim | upper }}" in macro_sql


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
    expected_quarantine = "cast('{{ target.database | upper }}.{{ var(\"target_schema\", \"BUSINESS\") | upper }}.ACCOUNT__QUARANTINE' as varchar(1024))"
    assert expected_quarantine in project["on-run-start"][1]
    assert expected_quarantine in project["on-run-end"][1]
    assert 'adapter.get_relation(database=(target.database | upper), schema=(var("target_schema", "BUSINESS") | upper), identifier=\'ACCOUNT__QUARANTINE\')' in project["on-run-end"][1]


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
    assert unit_test_yaml["unit_tests"][1]["given"][0]["format"] == "sql"
    assert unit_test_yaml["unit_tests"][1]["expect"]["rows"] == []


def test_scd2_generation_adds_business_data_hash(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
            """
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    not_implemented = (output_dir / "NOT_IMPLEMENTED.md").read_text(encoding="utf-8")
    assert "cast(sha2(concat_ws('|', coalesce(cast(cast(ACCOUNT_ID as varchar(20)) as varchar), '')), 256) as varchar(64)) as BUSINESS_DATA_HASH" in model_sql
    assert "SCD2 duplicate-hash historical boundary handling" not in not_implemented


def test_scd2_business_data_hash_include_uses_configured_fields(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                business_data_hash:
                  mode: include
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
    hash_line = next(line for line in model_sql.splitlines() if "BUSINESS_DATA_HASH" in line)
    assert "trim(ACCOUNT_NAME)" in hash_line
    assert "varchar(255)" in hash_line
    assert "ACCOUNT_ID" not in hash_line


def test_scd2_generation_adds_continuous_validity_windows(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                valid_from_datetime:
                  valid_from_datetime_selection: field
                  field: source_changed_at
                valid_to_datetime:
                  valid_to_datetime_selection: next
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
        - id: source_changed_at
          source:
            pos: 2
            column: source_changed_at
          data_type: timestamp_tz
            """,
        ),
    )

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "cast(SOURCE_CHANGED_AT as timestamp_tz) as TMS_VALID_FROM_DATETIME_CANDIDATE" in model_sql
    assert "row_number() over (partition by ACCOUNT_ID order by TMS_VALID_FROM_DATETIME_CANDIDATE) = 1" in model_sql
    assert "cast('0001-01-01T00:00:00Z' as timestamp_tz)" in model_sql
    assert "lead(VALID_FROM_DATETIME) over (partition by ACCOUNT_ID order by VALID_FROM_DATETIME)" in model_sql
    assert "dateadd(second, -1" not in model_sql
    assert "cast('9999-12-31T23:59:59Z' as timestamp_tz)" in model_sql
    assert "then 'Y'" in model_sql
    assert "else 'N'" in model_sql
    assert "end as IS_CURRENT_FLAG" in model_sql
    assert "'N' as IS_DELETED_FLAG" in model_sql


def test_scd2_generation_uses_field_delete_detection(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
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
    assert "case when ACCOUNT_STATUS = 'DELETED' then 'Y' else 'N' end as IS_DELETED_FLAG" in model_sql


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


def test_scd2_missing_from_source_queries_existing_target(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                delete_detection:
                  mode: missing_from_source
                valid_from_datetime:
                  valid_from_datetime_selection: load_datetime
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
    assert "materialized='incremental'" in model_sql
    assert "incremental_strategy='delete+insert'" in model_sql
    assert "unique_key=['ACCOUNT_ID']" in model_sql
    assert "from {{ this }}" in model_sql
    assert "current_target_rows as (" in model_sql
    assert "where IS_CURRENT_FLAG = 'Y'" in model_sql
    assert "source_change_rows as (" in model_sql
    assert "current_target.BUSINESS_DATA_HASH = typed_rows.BUSINESS_DATA_HASH" in model_sql
    assert "missing_from_source_delete_rows as (" in model_sql
    assert "'Y' as TMS_IS_DELETED_FLAG_CANDIDATE" in model_sql
    assert "where coalesce(current_target.IS_DELETED_FLAG, 'N') <> 'Y'" in model_sql
    assert "from incoming_key_rows as incoming_key" in model_sql
    assert "existing_target.VALID_FROM_DATETIME as TMS_VALID_FROM_DATETIME_CANDIDATE" in model_sql


def test_scd2_skip_mode_generates_duplicate_hash_boundary_handling(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                delete_detection:
                  mode: missing_from_source
                business_data_hash_duplicate_mode: skip
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
    assert "duplicate_boundary_rows as (" in model_sql
    assert "deduplicated_version_rows as (" in model_sql
    assert "TMS_NEXT_BUSINESS_DATA_HASH" in model_sql
    assert "TMS_NEXT_BUSINESS_DATA_HASH is not null" in model_sql
    assert "where not (TMS_IS_EXISTING_TARGET_ROW = 'N' and" in model_sql
    assert "SCD2 duplicate hash handling: current duplicate rows skipped=" in model_sql
    assert "SCD2 duplicate hash handling: historical duplicate rows skipped=" in model_sql


def test_scd2_update_mode_generates_duplicate_hash_boundary_updates(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                delete_detection:
                  mode: missing_from_source
                business_data_hash_duplicate_mode: update
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
    assert "business_data_hash_duplicate_mode" not in model_sql
    assert "TMS_PREVIOUS_IS_EXISTING_TARGET_ROW = 'N'" in model_sql
    assert "TMS_PREVIOUS_BUSINESS_DATA_HASH is not null" in model_sql
    assert "and not (TMS_PREVIOUS_2_BUSINESS_DATA_HASH is not null" in model_sql
    assert "SCD2 duplicate hash handling: historical duplicate boundaries updated=" in model_sql


def test_scd2_missing_from_source_unit_tests_override_is_incremental(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id,account_name\nACCT000000000001,Acme Trading\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                delete_detection:
                  mode: missing_from_source
                valid_from_datetime:
                  valid_from_datetime_selection: load_datetime
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
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    assert unit_test_yaml["unit_tests"][0]["overrides"] == {"macros": {"is_incremental": False}}


def test_scd2_generation_rejects_sparse_validity_mode(tmp_path: Path) -> None:
    result, _ = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                valid_from_to_mode: sparse
            """
        ),
    )

    assert diagnostic_messages(result.errors) == ["valid_from_to_mode `sparse` is not implemented yet"]
    assert result.errors[0].location == "$.control_data.scd.valid_from_to_mode"


def test_scd2_generation_rejects_missing_from_source_with_field_valid_from(tmp_path: Path) -> None:
    result, _ = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
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

    assert diagnostic_messages(result.errors) == [
        "delete_detection.mode `missing_from_source` is invalid when valid_from_datetime_selection is field"
    ]
    assert result.errors[0].location == "$.control_data.scd.delete_detection.mode"


def test_scd2_unit_tests_include_business_data_hash_expectation(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id,account_name\nACCT000000000001, Acme Trading \n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                business_data_hash:
                  mode: include
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
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    expected_row = unit_test_yaml["unit_tests"][0]["expect"]["rows"][0]
    assert expected_row["ACCOUNT_NAME"] == "Acme Trading"
    assert expected_row["BUSINESS_DATA_HASH"] == hashlib.sha256(b"Acme Trading").hexdigest()


def test_scd2_unit_tests_include_continuous_validity_windows(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text(
        "\n".join(
            [
                "account_id,account_name,source_changed_at",
                "ACCT000000000001,Acme Trading,2026-07-01T00:00:00Z",
                "ACCT000000000001,Acme Trading Plus,2026-07-05T00:00:00Z",
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
              change_type: scd2
              scd:
                business_key:
                  - account_id
                valid_from_datetime:
                  valid_from_datetime_selection: field
                  field: source_changed_at
                valid_to_datetime:
                  valid_to_datetime_selection: next
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
        - id: source_changed_at
          source:
            pos: 2
            column: source_changed_at
          data_type: timestamp_tz
            """,
        ),
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    expected_rows = unit_test_yaml["unit_tests"][0]["expect"]["rows"]
    assert expected_rows[0]["VALID_FROM_DATETIME"] == "0001-01-01T00:00:00Z"
    assert expected_rows[0]["VALID_TO_DATETIME"] == "2026-07-05T00:00:00Z"
    assert expected_rows[0]["IS_CURRENT_FLAG"] == "N"
    assert expected_rows[0]["IS_DELETED_FLAG"] == "N"
    assert expected_rows[1]["VALID_FROM_DATETIME"] == "2026-07-05T00:00:00Z"
    assert expected_rows[1]["VALID_TO_DATETIME"] == "9999-12-31T23:59:59Z"
    assert expected_rows[1]["IS_CURRENT_FLAG"] == "Y"
    assert expected_rows[1]["IS_DELETED_FLAG"] == "N"


def test_scd2_unit_tests_reject_timestamp_fixtures_without_timezone(tmp_path: Path) -> None:
    # Generated dbt unit-test fixtures should follow the same timestamp contract as source data.
    csv_path = tmp_path / "account.csv"
    csv_path.write_text(
        "\n".join(
            [
                "account_id,account_name,source_changed_at",
                "ACCT000000000001,Acme Trading,2026-07-01 00:00:00",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result, _ = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
                valid_from_datetime:
                  valid_from_datetime_selection: field
                  field: source_changed_at
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
        - id: source_changed_at
          source:
            pos: 2
            column: source_changed_at
          data_type: timestamp_tz
            """,
        ),
        unit_test_csv=csv_path,
    )

    assert diagnostic_messages(result.errors) == ["unit-test timestamp value must include a timezone"]
    assert result.errors[0].location == str(csv_path)


def test_scd2_unit_tests_include_field_delete_detection_flag(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id,account_status\nACCT000000000001,DELETED\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              change_type: scd2
              scd:
                business_key:
                  - account_id
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
        unit_test_csv=csv_path,
    )

    assert result.errors == []
    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "account_unit_tests.yml")
    expected_row = unit_test_yaml["unit_tests"][0]["expect"]["rows"][0]
    assert expected_row["IS_DELETED_FLAG"] == "Y"


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
