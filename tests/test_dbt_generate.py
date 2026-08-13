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
    assert "create table if not exists BUSINESS.TYPE_MATERIALISATION_JOBS" in project["on-run-start"][0]
    assert "'JOB_START'" in project["on-run-start"][1]
    assert len(project["on-run-end"]) == 1
    assert "'JOB_END'" in project["on-run-end"][0]
    assert 'var("job_result", "COMPLETED")' in project["on-run-end"][0]
    assert "results | selectattr('status', 'equalto', 'error')" in project["on-run-end"][0]
    assert "results | selectattr('status', 'equalto', 'fail')" in project["on-run-end"][0]
    assert "'FAILED'" in project["on-run-end"][0]
    assert "pre_hook" not in model_sql
    assert "post_hook" not in model_sql


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
