"""Tests for generating a runnable dbt project from a materialisation spec.

These checks cover CSV and table source models, generated model SQL, job hooks,
quarantine models, generated unit tests, and unsupported feature reporting.
"""

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


def test_csv_stage_location_omits_database_when_not_supplied(tmp_path: Path) -> None:
    # Omitted databases are resolved by dbt/Snowflake context, not by the generator.
    result, output_dir = generate(tmp_path, csv_generation_spec())

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "from @AD_HOC.csv_stage/account.csv" in source_sql


def test_csv_stage_location_includes_database_when_supplied(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec(extra_location="database: raw"))

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "from @raw.AD_HOC.csv_stage/account.csv" in source_sql


def test_csv_stage_override_wins_over_spec_location(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec(), csv_stage="raw.override_stage/override.csv")

    assert result.errors == []
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "from @raw.override_stage/override.csv" in source_sql
    assert "csv_stage/account.csv" not in source_sql


def test_csv_dbt_seed_generation_copies_seed_and_reads_from_ref(tmp_path: Path) -> None:
    seed_file = tmp_path / "account_seed_input.csv"
    seed_file.write_text("account_id,account_name\nACCT000000000001,Acme Trading\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        f"""
        id: account_csv
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
    assert seed_config["+column_types"] == {"account_id": "varchar", "account_name": "varchar"}
    source_sql = (output_dir / "models" / "generated" / "account__source.sql").read_text(encoding="utf-8")
    assert "from {{ ref('account_seed') }}" in source_sql
    assert "cast(account_id as string) as account_id" in source_sql
    assert "@csv_stage" not in source_sql


def test_csv_dbt_seed_generation_reports_missing_seed_file(tmp_path: Path) -> None:
    result, _ = generate(
        tmp_path,
        """
        id: account_csv
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
    assert "$1::string as account_id" in source_sql
    assert "$3::string as account_status" in source_sql


def test_generated_final_model_uses_audit_metadata_types(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec())

    assert result.errors == []
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert "cast('{{ var(\"audit_data_process_key\", \"manual\") }}' as varchar(64))" in model_sql
    assert "cast(current_timestamp() as datetime) as audit_created_datetime" in model_sql
    assert "cast(current_timestamp() as datetime) as audit_last_changed_datetime" in model_sql


def test_generated_project_uses_named_local_user_profile(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: account_csv
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


def test_fail_file_generates_validation_guard_model(tmp_path: Path) -> None:
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
    assert "where failure_details is not null" in guard_sql
    assert "fail_file validation failure enforcement" not in not_implemented


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
    assert "count(*) over (partition by try_cast(account_id as varchar(20))) > 1" in guard_sql
    assert "field `account_id` duplicates a value for a unique field" in guard_sql
    assert "uniqueness checks in generated dbt SQL" not in not_implemented


def test_table_source_generation_reads_from_configured_relation(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        """
        id: table_spec
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
    assert "account_id as account_id" in source_sql
    assert "from raw.landing.account_source" in source_sql


def test_job_event_hooks_are_generated_at_project_run_level(tmp_path: Path) -> None:
    result, output_dir = generate(tmp_path, csv_generation_spec())

    assert result.errors == []
    project = load_yaml(output_dir / "dbt_project.yml")
    model_sql = (output_dir / "models" / "generated" / "account.sql").read_text(encoding="utf-8")
    assert len(project["on-run-start"]) == 2
    assert "var('tms_enable_job_hooks', true)" in project["on-run-start"][0]
    assert "create table if not exists {{ var('tms_job_schema', 'BUSINESS') }}.TYPE_MATERIALISATION_JOBS" in project["on-run-start"][0]
    assert "details varchar(16777216)" in project["on-run-start"][0]
    assert "spec_file_name varchar(1024)" in project["on-run-start"][0]
    assert "generated_table varchar(1024)" in project["on-run-start"][0]
    assert "quarantine_table varchar(1024)" in project["on-run-start"][0]
    assert "loaded_count number(38, 0)" in project["on-run-start"][0]
    assert "quarantine_count number(38, 0)" in project["on-run-start"][0]
    assert "'JOB_START'" in project["on-run-start"][1]
    assert "details, spec_file_name, generated_table, quarantine_table, loaded_count, quarantine_count" in project["on-run-start"][1]
    assert "cast('spec.yaml' as varchar(1024))" in project["on-run-start"][1]
    assert "cast('{{ target.database }}.{{ var(\"target_schema\", \"business\") }}.account' as varchar(1024))" in project["on-run-start"][1]
    assert "cast(null as varchar(1024))" in project["on-run-start"][1]
    assert "cast(null as number(38, 0))" in project["on-run-start"][1]
    assert len(project["on-run-end"]) == 2
    assert "create table if not exists {{ var('tms_job_schema', 'BUSINESS') }}.TYPE_MATERIALISATION_JOBS" in project["on-run-end"][0]
    assert "var('tms_enable_job_hooks', true)" in project["on-run-end"][1]
    assert "'JOB_END'" in project["on-run-end"][1]
    assert 'var("job_result", "COMPLETED")' not in project["on-run-end"][1]
    assert 'var("job_details", none)' in project["on-run-end"][1]
    assert "case when quarantine_counts.quarantine_count > 0 then 'COMPLETED_WITH_QUARANTINE' else 'COMPLETED' end" in project["on-run-end"][1]
    assert "'COMPLETED_WITH_QUARANTINE'" in project["on-run-end"][1]
    assert "case when quarantine_counts.quarantine_count > 0 then 'validation errors written to quarantine output' else null end" in project["on-run-end"][1]
    assert 'adapter.get_relation(database=target.database, schema=var("target_schema", "business"), identifier=\'account\')' in project["on-run-end"][1]
    assert "loaded_counts as (select {% if tms_generated_relation is not none %}(select count(*) from {{ tms_generated_relation }}){% else %}null{% endif %} as loaded_count)" in project["on-run-end"][1]
    assert "{% set tms_quarantine_relation = none %}" in project["on-run-end"][1]
    assert "quarantine_counts as (select {% if tms_quarantine_relation is not none %}(select count(*) from {{ tms_quarantine_relation }}){% else %}null{% endif %} as quarantine_count)" in project["on-run-end"][1]
    assert "loaded_counts.loaded_count" in project["on-run-end"][1]
    assert "quarantine_counts.quarantine_count" in project["on-run-end"][1]
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
    assert "{{ override_schema | trim }}" in macro_sql


def test_job_event_hooks_include_quarantine_relation_when_enabled(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              failure_mode: quarantine_row
            """
        ),
    )

    assert result.errors == []
    project = load_yaml(output_dir / "dbt_project.yml")
    expected_quarantine = "cast('{{ target.database }}.{{ var(\"target_schema\", \"business\") }}.account_QUARANTINE' as varchar(1024))"
    assert expected_quarantine in project["on-run-start"][1]
    assert expected_quarantine in project["on-run-end"][1]
    assert 'adapter.get_relation(database=target.database, schema=var("target_schema", "business"), identifier=\'account_QUARANTINE\')' in project["on-run-end"][1]


def test_quarantine_model_is_incremental_and_append_only(tmp_path: Path) -> None:
    # The quarantine relation may expand for new source fields, but must not shrink existing columns.
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
              failure_mode: quarantine_row
            """
        ),
    )

    assert result.errors == []
    quarantine_sql = (output_dir / "models" / "generated" / "account__quarantine.sql").read_text(encoding="utf-8")
    assert "materialized='incremental'" in quarantine_sql
    assert "incremental_strategy='append'" in quarantine_sql
    assert "on_schema_change='append_new_columns'" in quarantine_sql
    assert "alias='account_QUARANTINE'" in quarantine_sql
    assert "failure_details" in quarantine_sql


def test_quarantine_model_uses_generated_uniqueness_validation(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
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
    assert "count(*) over (partition by try_cast(account_id as varchar(20))) > 1" in quarantine_sql
    assert "from validation_rows" in quarantine_sql


def test_quarantine_config_defaults_can_be_overridden(tmp_path: Path) -> None:
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
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
    assert "database='ops'" in quarantine_sql
    assert "schema='data_quality'" in quarantine_sql
    assert "alias='account_bad_rows'" in quarantine_sql


def test_generated_unit_tests_include_final_and_quarantine_models(tmp_path: Path) -> None:
    csv_path = tmp_path / "account.csv"
    csv_path.write_text("account_id\nACCT000000000001\n", encoding="utf-8")
    result, output_dir = generate(
        tmp_path,
        csv_generation_spec(
            extra_control="""
            control_data:
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
    assert "cast('ACCT000000000001' as varchar) as account_id" in unit_test_yaml["unit_tests"][0]["given"][0]["rows"]
    assert unit_test_yaml["unit_tests"][1]["given"][0]["format"] == "sql"
    assert unit_test_yaml["unit_tests"][1]["expect"]["rows"] == []


def test_scd2_generation_fails_cleanly_for_now(tmp_path: Path) -> None:
    result, _ = generate(
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

    assert diagnostic_messages(result.errors) == ["SCD materialisation is not implemented yet"]
    assert result.errors[0].location == "$.control_data.change_type"


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
