from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from type_materialisation.schema import load_yaml

ROOT = Path(__file__).resolve().parents[1]

from tms_integration.assertions import normalise_rows, read_csv_rows
from tms_integration import dbt_runner
from tms_integration import database as integration_database
from tms_integration import reporting as integration_reporting
from tms_integration.database import (
    DEFAULT_SNOWFLAKE_CONNECTION,
    IntegrationConfigError,
    drop_relations,
    drop_stages,
    fetch_relation_rows,
    insert_csv_rows,
    load_snowflake_connection_config,
    replace_csv_stage_from_file,
    replace_source_table_from_csv,
    replace_source_table_from_json,
)
from tms_integration.runner import (
    CustomAssertionContext,
    _force_runtime_failure_model,
    _generated_relation_names,
    _generated_stage_names,
    _keep_tables_enabled,
    _load_custom_assertions,
    _source_csv_stage_name,
    _source_format,
    _source_load_method,
    _source_table_name,
    _table_prefix,
    _target_schema_name,
    generate_project_for_scenario,
)
from tms_integration.reporting import IntegrationReporter  # noqa: E402
from tms_integration.scenario import discover_scenarios, load_scenario  # noqa: E402


INTEGRATION_ROOT = ROOT / "integration_tests"


class RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class RecordingCursor:
    def __init__(self):
        self.executed: list[tuple[str, tuple[str, ...]]] = []
        self.executemany_called = False
        self.closed = False
        self.description = []
        self.rows = []

    def execute(self, sql, values=None):
        self.executed.append((sql, values))

    def executemany(self, sql, values):
        del sql, values
        self.executemany_called = True

    def close(self):
        self.closed = True

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


def test_integration_scenarios_are_discoverable_and_self_contained() -> None:
    scenario_roots = discover_scenarios(INTEGRATION_ROOT)

    assert [path.name for path in scenario_roots] == [
        "job_details_failure_modes",
        "scd1_csv_date_timestamp_formats",
        "scd1_csv_stage_load",
        "scd1_csv_transforms",
        "scd1_quarantine_regex_validation",
        "scd1_table_source_json_flatten",
        "scd1_table_source_query_quarantine",
        "scd1_table_source_varchar_load",
        "scd2_continuous_field_validity",
        "scd2_derived_customer_bronze_silver_gold",
        "scd2_derived_future_closed_record",
        "scd2_derived_out_of_order_multi_history",
        "scd2_derived_same_effective_datetime_dedup",
        "scd2_derived_source_current_flag_preserved",
        "scd2_hash_exclude_source_field",
        "scd2_hash_skip_current_duplicate",
        "scd2_hash_update_historical_boundary",
        "scd2_manual_multiple_current_failure_rollback",
        "scd2_manual_overlap_failure_rollback",
        "scd2_missing_from_source_delete",
        "scd2_validation_continuous_failure_rollback",
        "scd2_validation_continuous_gap_failure_rollback",
        "scd2_validation_sparse_failure_rollback",
    ]
    for scenario_root in scenario_roots:
        scenario = load_scenario(scenario_root)
        assert scenario.readme.exists()
        assert scenario.spec_path.exists()
        if scenario.dbt_unit_test_csv is not None:
            assert scenario.dbt_unit_test_csv.exists()
        assert scenario.checks
        assert scenario.loads
        assert (scenario.root / "src").is_dir()
        for step in scenario.loads:
            assert (step.source_csv is None) != (step.source_json is None)
            if step.source_csv is not None:
                assert step.source_csv.exists()
            if step.source_json is not None:
                assert step.source_json.exists()
            assert isinstance(step.force_runtime_failure, bool)
            if step.expect_dbt_success:
                assert step.expected_target_csv is not None
            if step.expected_target_csv is not None:
                assert step.expected_target_csv.exists()
            assert isinstance(step.dbt_vars, dict)
            for expected_relation in step.expected_relations:
                assert expected_relation.expected_csv.exists()
                assert expected_relation.columns
    assert (INTEGRATION_ROOT / "scd1_csv_stage_load" / "src" / "assertions.py").is_file()
    assert (INTEGRATION_ROOT / "scd2_hash_skip_current_duplicate" / "src" / "assertions.py").is_file()


def test_integration_scenarios_generate_dbt_unit_tests(tmp_path: Path) -> None:
    for scenario_root in discover_scenarios(INTEGRATION_ROOT):
        scenario = load_scenario(scenario_root)
        if scenario.dbt_unit_test_csv is None:
            continue
        output_dir = tmp_path / scenario.name

        generated_project = generate_project_for_scenario(scenario, output_dir)

        unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "tms_int__account_unit_tests.yml")
        assert generated_project.spec_path.name == f"{scenario.name}__prefixed_spec.yaml"
        assert unit_test_yaml["unit_tests"][0]["model"] == "tms_int__account"
        assert unit_test_yaml["unit_tests"][0]["given"][0]["input"] == "ref('tms_int__account__source')"
        assert unit_test_yaml["unit_tests"][0]["given"][0]["format"] == "sql"
        assert unit_test_yaml["unit_tests"][0]["given"][1]["input"] == "ref('tms_int__account__validation_guard')"
        assert "VALIDATION_FAILURE_GUARD" in unit_test_yaml["unit_tests"][0]["given"][1]["rows"]
        if scenario.name == "scd2_missing_from_source_delete":
            assert unit_test_yaml["unit_tests"][0]["overrides"] == {
                "vars": {"tms_unit_test": True},
                "macros": {"is_incremental": False},
            }
        else:
            assert unit_test_yaml["unit_tests"][0]["overrides"]["vars"] == {"tms_unit_test": True}
        assert unit_test_yaml["unit_tests"][0]["expect"]["rows"]


def test_live_reporter_prints_scenario_steps_and_checks() -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd2_missing_from_source_delete")
    output = StringIO()
    reporter = IntegrationReporter(Console(file=output, force_terminal=False), enabled=True)

    reporter.scenario_start(scenario, schema="TMP", prefix="TMS_INT__")
    reporter.step("Setting up initial target table", scenario.initial_target_csv.name)
    reporter.checks(scenario.checks)
    reporter.ok("Expected target rows matched", "3 rows")

    text = output.getvalue()
    normalized_text = " ".join(text.split())
    assert text.startswith("\n\n")
    assert "scd2_missing_from_source_delete" in text
    assert "Setting up initial target table" in text
    assert (
        "A1 remains current because SCD2 auto does not infer missing-from-source deletes."
        in normalized_text
    )
    assert "Expected target rows matched" in text


def test_live_reporter_prints_clear_failure_panel() -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd2_missing_from_source_delete")
    output = StringIO()
    reporter = IntegrationReporter(Console(file=output, force_terminal=False), enabled=True)

    reporter.scenario_start(scenario, schema="TMP", prefix="TMS_INT__")
    reporter.step("Running tms dbt-build")
    reporter.scenario_failed(scenario, RuntimeError("dbt build failed\nextra details later"))

    text = output.getvalue()
    assert "TMS integration failed" in text
    assert "scd2_missing_from_source_delete" in text
    assert "last step: Running tms dbt-build" in text
    assert "RuntimeError: dbt build failed" in text
    assert "pytest traceback follows below" in text


def test_live_reporter_closes_owned_output(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(integration_reporting, "_terminal_output", lambda: output)

    reporter = IntegrationReporter(enabled=True)
    reporter.step("Generating dbt project")
    reporter.close()

    assert output.closed is True


def test_live_runner_defaults_to_tmp_schema(monkeypatch) -> None:
    monkeypatch.delenv("TMS_INTEGRATION_SCHEMA", raising=False)

    assert _target_schema_name() == "TMP"


def test_live_runner_defaults_to_integration_table_prefix(monkeypatch) -> None:
    monkeypatch.delenv("TMS_INTEGRATION_TABLE_PREFIX", raising=False)

    assert _table_prefix() == "TMS_INT__"


def test_live_runner_requires_table_prefix_to_end_with_double_underscore(monkeypatch) -> None:
    monkeypatch.setenv("TMS_INTEGRATION_TABLE_PREFIX", "TMS_INT")

    try:
        _table_prefix()
    except AssertionError as exc:
        assert "end with `__`" in str(exc)
    else:
        raise AssertionError("expected AssertionError")


def test_live_runner_requires_table_prefix_to_be_at_least_three_characters(monkeypatch) -> None:
    monkeypatch.setenv("TMS_INTEGRATION_TABLE_PREFIX", "__")

    try:
        _table_prefix()
    except AssertionError as exc:
        assert "at least 3 characters" in str(exc)
    else:
        raise AssertionError("expected AssertionError")


def test_live_runner_cleans_up_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TMS_INTEGRATION_KEEP_TABLES", raising=False)

    assert _keep_tables_enabled() is False


def test_live_runner_can_keep_tables_for_inspection(monkeypatch) -> None:
    monkeypatch.setenv("TMS_INTEGRATION_KEEP_TABLES", "1")

    assert _keep_tables_enabled() is True


def test_live_runner_cleanup_ignores_snowflake_relation_type_mismatch(monkeypatch) -> None:
    commands: list[str] = []

    def fake_execute(connection, sql):
        del connection
        commands.append(sql)
        if sql.startswith("drop view"):
            raise integration_database.snowflake.connector.errors.ProgrammingError(
                msg="SQL compilation error: Object found is of type 'TABLE', not specified type 'VIEW'.",
                errno=2203,
            )

    monkeypatch.setattr(integration_database, "_execute", fake_execute)

    drop_relations(object(), "TMP", ["TMS_INT__ACCOUNT"])

    assert commands == [
        "drop view if exists TMP.TMS_INT__ACCOUNT",
        "drop table if exists TMP.TMS_INT__ACCOUNT",
    ]


def test_live_runner_drops_stage_relations(monkeypatch) -> None:
    commands: list[str] = []
    monkeypatch.setattr(integration_database, "_execute", lambda connection, sql: commands.append(sql))

    drop_stages(object(), "TMP", ["TMS_INT__CSV_STAGE"])

    assert commands == ["drop stage if exists TMP.TMS_INT__CSV_STAGE"]


def test_initial_target_load_uses_single_row_inserts(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
target:
  id: account
  schema: business
  fields:
    - id: account_id
      data_type: varchar(20)
    - id: account_name
      data_type: varchar(255)
        """,
        encoding="utf-8",
    )
    csv_path = tmp_path / "initial_target.csv"
    csv_path.write_text("ACCOUNT_ID,ACCOUNT_NAME\nA1,One\nA2,Two\n", encoding="utf-8")
    cursor = RecordingCursor()

    insert_csv_rows(RecordingConnection(cursor), "TMP", "TMS_INT__ACCOUNT", csv_path, spec_path)

    assert len(cursor.executed) == 2
    assert cursor.executed[0][1] == ("A1", "One")
    assert cursor.executed[1][1] == ("A2", "Two")
    assert cursor.executemany_called is False
    assert cursor.closed is True


def test_source_table_load_creates_varchar_table_from_csv(monkeypatch, tmp_path: Path) -> None:
    csv_path = tmp_path / "source.csv"
    csv_path.write_text("account_id,account_priority\nA1,10\nA2,20\n", encoding="utf-8")
    cursor = RecordingCursor()
    ddl: list[str] = []

    monkeypatch.setattr(integration_database, "_execute", lambda connection, sql: ddl.append(sql))

    replace_source_table_from_csv(RecordingConnection(cursor), "TMP", "TMS_INT__ACCOUNT_SOURCE_TABLE", csv_path)

    assert ddl == [
        "create or replace table TMP.TMS_INT__ACCOUNT_SOURCE_TABLE (ACCOUNT_ID varchar, ACCOUNT_PRIORITY varchar)"
    ]
    assert cursor.executed == [
        (
            "insert into TMP.TMS_INT__ACCOUNT_SOURCE_TABLE (ACCOUNT_ID, ACCOUNT_PRIORITY) values (%s, %s)",
            ("A1", "10"),
        ),
        (
            "insert into TMP.TMS_INT__ACCOUNT_SOURCE_TABLE (ACCOUNT_ID, ACCOUNT_PRIORITY) values (%s, %s)",
            ("A2", "20"),
        ),
    ]
    assert cursor.closed is True


def test_source_table_load_creates_variant_payload_from_json(monkeypatch, tmp_path: Path) -> None:
    json_path = tmp_path / "source.json"
    json_path.write_text('[{"customer":{"id":"A1"}},{"customer":{"id":"A2"}}]\n', encoding="utf-8")
    cursor = RecordingCursor()
    ddl: list[str] = []

    monkeypatch.setattr(integration_database, "_execute", lambda connection, sql: ddl.append(sql))

    replace_source_table_from_json(RecordingConnection(cursor), "TMP", "TMS_INT__JSON_SOURCE", json_path)

    assert ddl == ["create or replace table TMP.TMS_INT__JSON_SOURCE (PAYLOAD variant)"]
    assert cursor.executed == [
        ("insert into TMP.TMS_INT__JSON_SOURCE (PAYLOAD) select parse_json(%s)", ('{"customer":{"id":"A1"}}',)),
        ("insert into TMP.TMS_INT__JSON_SOURCE (PAYLOAD) select parse_json(%s)", ('{"customer":{"id":"A2"}}',)),
    ]
    assert cursor.closed is True


def test_csv_stage_load_creates_stage_and_puts_file(monkeypatch, tmp_path: Path) -> None:
    csv_path = tmp_path / "load_001_source.csv"
    csv_path.write_text("A1,One\n", encoding="utf-8")
    commands: list[str] = []
    monkeypatch.setattr(integration_database, "_execute", lambda connection, sql: commands.append(sql))

    replace_csv_stage_from_file(object(), "TMP", "TMS_INT__CSV_STAGE", csv_path)

    assert commands == [
        (
            "create or replace stage TMP.TMS_INT__CSV_STAGE "
            "file_format = (type = csv field_delimiter = ',' skip_header = 0 "
            "field_optionally_enclosed_by = '\"')"
        ),
        f"put '{csv_path.resolve().as_uri()}' @TMP.TMS_INT__CSV_STAGE auto_compress=false overwrite=true",
    ]


def test_fetch_relation_rows_casts_requested_columns_to_varchar() -> None:
    cursor = RecordingCursor()
    cursor.description = [("FAILURE_DETAILS",), ("ACCOUNT_ID",)]
    cursor.rows = [("field `account_id` does not match regex", "BAD3")]

    rows = fetch_relation_rows(
        RecordingConnection(cursor),
        "TMP",
        "TMS_INT__ACCOUNT__QUARANTINE",
        ["FAILURE_DETAILS", "ACCOUNT_ID"],
        ["ACCOUNT_ID"],
    )

    assert rows == [{"FAILURE_DETAILS": "field `account_id` does not match regex", "ACCOUNT_ID": "BAD3"}]
    assert cursor.executed == [
        (
            "select cast(FAILURE_DETAILS as varchar) as FAILURE_DETAILS, "
            "cast(ACCOUNT_ID as varchar) as ACCOUNT_ID "
            "from TMP.TMS_INT__ACCOUNT__QUARANTINE order by ACCOUNT_ID",
            None,
        )
    ]
    assert cursor.closed is True


def test_integration_dbt_runner_uses_tms_dbt_build(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    spec_path = tmp_path / "spec.yaml"
    project_dir = tmp_path / "project"
    spec_path.write_text("id: account\n", encoding="utf-8")

    monkeypatch.setattr(
        dbt_runner.shutil,
        "which",
        lambda executable: f"/venv/bin/{executable}" if executable in {"tms", "dbt"} else None,
    )

    def fake_run(command, *, check, capture_output, text):
        commands.append(command)
        assert check is False
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(dbt_runner.subprocess, "run", fake_run)

    dbt_runner.run_tms_dbt_project(
        spec_path=spec_path,
        project_dir=project_dir,
        target_schema="TMP",
        dbt_vars={"insert_time": "2026-09-02T00:00:00Z"},
    )

    assert commands == [
        [
            "/venv/bin/tms",
            "dbt-build",
            "--spec",
            str(spec_path),
            "--project-dir",
            str(project_dir),
            "--target",
            "dev",
            "--vars",
            json.dumps(
                {
                    "target_schema": "TMP",
                    "tms_job_schema": "TMP",
                    "tms_staging_schema": "TMP",
                    "insert_time": "2026-09-02T00:00:00Z",
                }
            ),
        ]
    ]


def test_integration_dbt_runner_can_return_expected_failure(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    project_dir = tmp_path / "project"
    spec_path.write_text("id: account\n", encoding="utf-8")

    monkeypatch.setattr(
        dbt_runner.shutil,
        "which",
        lambda executable: f"/venv/bin/{executable}" if executable in {"tms", "dbt"} else None,
    )
    monkeypatch.setattr(
        dbt_runner.subprocess,
        "run",
        lambda command, *, check, capture_output, text: subprocess.CompletedProcess(
            command,
            1,
            stdout="dbt failed",
            stderr="",
        ),
    )

    result = dbt_runner.run_tms_dbt_project(
        spec_path=spec_path,
        project_dir=project_dir,
        target_schema="TMP",
        require_success=False,
    )

    assert result.returncode == 1
    assert result.stdout == "dbt failed"


def test_integration_dbt_runner_reports_missing_dbt_before_running_tms(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    project_dir = tmp_path / "project"
    spec_path.write_text("id: account\n", encoding="utf-8")

    monkeypatch.setattr(dbt_runner.shutil, "which", lambda executable: "/venv/bin/tms" if executable == "tms" else None)

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called when dbt is missing")

    monkeypatch.setattr(dbt_runner.subprocess, "run", fail_run)

    with pytest.raises(dbt_runner.TmsCommandError, match="dbt executable was not found on PATH"):
        dbt_runner.run_tms_dbt_project(
            spec_path=spec_path,
            project_dir=project_dir,
            target_schema="TMP",
        )


def test_snowflake_connection_config_defaults_to_tms_int(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[connections.lfsprod]
account = "prod_account"
user = "prod_user"
warehouse = "prod_warehouse"
database = "prod_database"

[connections.tms_int]
account = "account"
user = "user"
warehouse = "warehouse"
database = "database"
role = "role"
authenticator = "externalbrowser"
unused = "ignored"
        """,
        encoding="utf-8",
    )

    config = load_snowflake_connection_config(config_path=config_path)

    assert DEFAULT_SNOWFLAKE_CONNECTION == "tms_int"
    assert config == {
        "account": "account",
        "user": "user",
        "warehouse": "warehouse",
        "database": "database",
        "role": "role",
        "authenticator": "externalbrowser",
    }


def test_snowflake_connection_config_uses_explicit_connection_name_when_tms_int_is_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
default_connection_name = "lfsprod"

[connections.lfsprod]
account = "account"
user = "user"
warehouse = "warehouse"
database = "database"
        """,
        encoding="utf-8",
    )

    config = load_snowflake_connection_config(config_path=config_path, connection_name="lfsprod")

    assert config["account"] == "account"


def test_snowflake_connection_config_can_select_connection_from_env(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[connections.dev]
account = "dev_account"
user = "dev_user"
warehouse = "dev_warehouse"
database = "dev_database"

[connections.lfsprod]
accountname = "prod_account"
username = "prod_user"
warehousename = "prod_warehouse"
dbname = "prod_database"
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("TMS_SNOWFLAKE_CONNECTION", "lfsprod")

    config = load_snowflake_connection_config(config_path=config_path)

    assert config["account"] == "prod_account"
    assert config["user"] == "prod_user"
    assert config["warehouse"] == "prod_warehouse"
    assert config["database"] == "prod_database"


def test_snowflake_connection_config_errors_when_file_is_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"

    try:
        load_snowflake_connection_config(config_path=config_path)
    except IntegrationConfigError as exc:
        message = str(exc)
        assert "Snowflake config file was not found" in message
        assert "[connections.tms_int]" in message
    else:
        raise AssertionError("expected IntegrationConfigError")


def test_snowflake_connection_config_errors_when_tms_int_profile_is_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[connections.dev]
account = "dev_account"
user = "dev_user"
warehouse = "dev_warehouse"
database = "dev_database"

[connections.prod]
account = "prod_account"
user = "prod_user"
warehouse = "prod_warehouse"
database = "prod_database"
        """,
        encoding="utf-8",
    )

    try:
        load_snowflake_connection_config(config_path=config_path)
    except IntegrationConfigError as exc:
        message = str(exc)
        assert "connection profile `tms_int` was not found" in message
        assert "set TMS_SNOWFLAKE_CONNECTION" in message
    else:
        raise AssertionError("expected IntegrationConfigError")


def test_snowflake_connection_config_errors_when_selected_profile_is_missing(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[connections.tms_int]
account = "account"
user = "user"
warehouse = "warehouse"
database = "database"
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("TMS_SNOWFLAKE_CONNECTION", "lfsprod")

    try:
        load_snowflake_connection_config(config_path=config_path)
    except IntegrationConfigError as exc:
        message = str(exc)
        assert "connection profile `lfsprod` was not found" in message
        assert "available" not in message.lower()
        assert "tms_int" in message
    else:
        raise AssertionError("expected IntegrationConfigError")


def test_live_runner_identifies_generated_relations_for_cleanup(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd2_missing_from_source_delete")
    output_dir = tmp_path / scenario.name

    generated_project = generate_project_for_scenario(scenario, output_dir)

    relation_names = _generated_relation_names(generated_project.spec_path)

    assert "TMS_INT__ACCOUNT" in relation_names
    assert "TMS_INT__ACCOUNT__SOURCE" in relation_names
    assert "TMS_INT__ACCOUNT_SOURCE_SEED" in relation_names
    assert "TMS_INT__ACCOUNT__QUARANTINE" in relation_names
    assert "TMS_INT__TYPE_MATERIALISATION_JOBS" in relation_names


def test_table_source_scenario_prefixes_source_table_and_adds_it_to_cleanup(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd1_table_source_varchar_load")
    output_dir = tmp_path / scenario.name

    generated_project = generate_project_for_scenario(scenario, output_dir)

    spec = load_yaml(generated_project.spec_path)
    relation_names = _generated_relation_names(generated_project.spec_path)
    source_sql = (
        output_dir / "models" / "generated" / "tms_int__account__source.sql"
    ).read_text(encoding="utf-8")
    assert spec["source"]["schema"] == "{{ var('target_schema', 'TMP') }}"
    assert spec["source"]["table"] == "tms_int__account_source_table"
    assert _source_format(generated_project.spec_path) == "table"
    assert _source_table_name(generated_project.spec_path) == "TMS_INT__ACCOUNT_SOURCE_TABLE"
    assert "TMS_INT__ACCOUNT_SOURCE_TABLE" in relation_names
    assert "from {{ var('target_schema', 'TMP') }}.tms_int__account_source_table" in spec["source"]["query"]
    assert "from {{ var('target_schema', 'TMP') }}.tms_int__account_source_table" in source_sql
    assert "where load_batch_id = 'LOAD_001'" in source_sql


def test_table_source_query_quarantine_scenario_projects_source_query(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd1_table_source_query_quarantine")
    output_dir = tmp_path / scenario.name

    generated_project = generate_project_for_scenario(scenario, output_dir)

    spec = load_yaml(generated_project.spec_path)
    relation_names = _generated_relation_names(generated_project.spec_path)
    source_sql = (
        output_dir / "models" / "generated" / "tms_int__account__source.sql"
    ).read_text(encoding="utf-8")
    quarantine_sql = (
        output_dir / "models" / "generated" / "tms_int__account__quarantine.sql"
    ).read_text(encoding="utf-8")
    assert spec["source"]["table"] == "tms_int__account_query_quarantine_source"
    assert "TMS_INT__ACCOUNT_QUERY_QUARANTINE_SOURCE" in relation_names
    assert "select account_id, account_name, account_priority" in source_sql
    assert "load_batch_id" in source_sql
    assert "LOAD_001" in source_sql
    assert "LOAD_999" not in source_sql
    assert "LOAD_BATCH_ID as LOAD_BATCH_ID" not in source_sql
    assert "LOAD_BATCH_ID" not in quarantine_sql
    assert "where FAILURE_DETAILS is not null" in quarantine_sql


def test_table_source_json_flatten_scenario_generates_native_snowflake_extraction(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd1_table_source_json_flatten")
    output_dir = tmp_path / scenario.name

    generated_project = generate_project_for_scenario(scenario, output_dir)

    source_sql = (
        output_dir / "models" / "generated" / "tms_int__account_order__source.sql"
    ).read_text(encoding="utf-8")
    model_sql = (
        output_dir / "models" / "generated" / "tms_int__account_order.sql"
    ).read_text(encoding="utf-8")
    assert scenario.loads[0].source_json is not None
    assert "to_varchar(get_path(source_query.PAYLOAD, 'customer.account.id')) as ACCOUNT_ID" in source_sql
    assert "to_varchar(get_path(source_query.PAYLOAD, 'customer.profile.name')) as CUSTOMER_NAME" in source_sql
    assert "to_varchar(get_path(ORDER_ITEM.value, 'metrics.amount')) as ORDER_AMOUNT" in source_sql
    assert ", lateral flatten(input => get_path(source_query.PAYLOAD, 'customer.orders'), mode => 'ARRAY') as ORDER_ITEM" in source_sql
    assert "try_to_date(cast(ORDER_DATE as varchar), 'YYYY-MM-DD')" in model_sql


def test_csv_stage_scenario_prefixes_stage_and_adds_it_to_cleanup(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd1_csv_stage_load")
    output_dir = tmp_path / scenario.name

    generated_project = generate_project_for_scenario(scenario, output_dir)

    spec = load_yaml(generated_project.spec_path)
    stage_names = _generated_stage_names(generated_project.spec_path)
    source_sql = (
        output_dir / "models" / "generated" / "tms_int__account__source.sql"
    ).read_text(encoding="utf-8")
    assert spec["source"]["location"]["schema"] == "{{ var('target_schema', 'TMP') }}"
    assert spec["source"]["location"]["stage"] == "tms_int__csv_stage"
    assert _source_format(generated_project.spec_path) == "csv"
    assert _source_load_method(generated_project.spec_path) == "stage"
    assert _source_csv_stage_name(generated_project.spec_path) == "TMS_INT__CSV_STAGE"
    assert stage_names == ["TMS_INT__CSV_STAGE"]
    assert "from @{{ var('target_schema', 'TMP') }}.TMS_INT__CSV_STAGE/load_001_source.csv" in source_sql


def test_csv_stage_scenario_custom_src_assertion_runs() -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd1_csv_stage_load")
    assertions = _load_custom_assertions(scenario.root)
    cursor = RecordingCursor()
    cursor.rows = [(2, 30)]

    assertions.assert_after_load(
        CustomAssertionContext(
            connection=RecordingConnection(cursor),
            scenario_name=scenario.name,
            load_name="load_001",
            target_schema="TMP",
            table_prefix="TMS_INT__",
            target_table="TMS_INT__ACCOUNT",
            project_dir=INTEGRATION_ROOT,
            spec_path=scenario.spec_path,
            source_csv=scenario.loads[0].source_csv,
        )
    )

    assert cursor.executed == [
        (
            "select count(*) as ROW_COUNT, sum(ACCOUNT_PRIORITY) as PRIORITY_TOTAL "
            "from TMP.TMS_INT__ACCOUNT",
            None,
        )
    ]
    assert cursor.closed is True


def test_csv_transform_scenario_generates_transform_sql(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd1_csv_transforms")
    output_dir = tmp_path / scenario.name

    generate_project_for_scenario(scenario, output_dir)

    model_sql = (output_dir / "models" / "generated" / "tms_int__account.sql").read_text(encoding="utf-8")
    assert "cast(trim(ACCOUNT_ID) as varchar(20)) as ACCOUNT_ID" in model_sql
    assert "cast(trim(ACCOUNT_NAME) as varchar(255)) as ACCOUNT_NAME" in model_sql
    assert "cast(ltrim(LEFT_TRIM_CODE) as varchar(50)) as LEFT_TRIM_CODE" in model_sql
    assert "cast(rtrim(RIGHT_TRIM_CODE) as varchar(50)) as RIGHT_TRIM_CODE" in model_sql
    assert "cast(round(ROUNDED_AMOUNT, 2) as number(10,2)) as ROUNDED_AMOUNT" in model_sql


def test_csv_date_timestamp_scenario_generates_snowflake_parse_sql(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd1_csv_date_timestamp_formats")
    output_dir = tmp_path / scenario.name

    generate_project_for_scenario(scenario, output_dir)

    model_sql = (output_dir / "models" / "generated" / "tms_int__account.sql").read_text(encoding="utf-8")
    assert "try_to_date(cast(OPENED_ON_ISO as varchar), 'YYYY-MM-DD')" in model_sql
    assert "try_to_date(cast(OPENED_ON_AU as varchar), 'DD/MM/YYYY')" in model_sql
    assert "try_to_date(cast(OPENED_ON_NAMED as varchar), 'DD-MON-YYYY')" in model_sql
    assert "try_to_timestamp_tz(cast(OPENED_AT_OFFSET as varchar), 'YYYY-MM-DD\"T\"HH24:MI:SSTZHTZM')" in model_sql
    assert "try_to_timestamp_tz(concat(cast(REVIEWED_AT_UTC as varchar), ' +0000'), 'YYYY-MM-DD HH24:MI:SS TZHTZM')" in model_sql


def test_quarantine_scenario_generates_regex_validation_and_quarantine_alias(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd1_quarantine_regex_validation")
    output_dir = tmp_path / scenario.name

    generate_project_for_scenario(scenario, output_dir)

    model_sql = (output_dir / "models" / "generated" / "tms_int__account.sql").read_text(encoding="utf-8")
    quarantine_sql = (
        output_dir / "models" / "generated" / "tms_int__account__quarantine.sql"
    ).read_text(encoding="utf-8")
    assert "regexp_like(ACCOUNT_ID, '^A[0-9]{3}$')" in model_sql
    assert "where FAILURE_DETAILS is null" in model_sql
    assert "alias='TMS_INT__ACCOUNT__QUARANTINE'" in quarantine_sql
    assert "where FAILURE_DETAILS is not null" in quarantine_sql


def test_job_failure_scenario_generates_fail_load_validation_guard(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "job_details_failure_modes")
    output_dir = tmp_path / scenario.name

    generate_project_for_scenario(scenario, output_dir)

    model_sql = (output_dir / "models" / "generated" / "tms_int__account.sql").read_text(encoding="utf-8")
    guard_sql = (
        output_dir / "models" / "generated" / "tms_int__account__validation_guard.sql"
    ).read_text(encoding="utf-8")
    assert "ref('tms_int__account__validation_guard')" in model_sql
    assert "TYPE_MATERIALISATION_VALIDATION_FAILED:" in model_sql
    assert "pre_hook='drop table if exists {{ this }}'" in guard_sql
    assert "VALIDATION_FAILURE_COUNT" in guard_sql
    assert "VALIDATION_FAILURE_DETAILS" in guard_sql
    assert "field `account_id` does not match regex" in guard_sql


def test_job_failure_scenario_can_force_runtime_model_failure(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "job_details_failure_modes")
    output_dir = tmp_path / scenario.name
    generated_project = generate_project_for_scenario(scenario, output_dir)

    _force_runtime_failure_model(output_dir, generated_project.spec_path)

    model_sql = (output_dir / "models" / "generated" / "tms_int__account.sql").read_text(encoding="utf-8")
    assert "TMS_INT__RELATION_THAT_DOES_NOT_EXIST_FOR_RUNTIME_FAILURE" in model_sql
    assert "{{ config(materialized='table') }}" in model_sql


def test_hash_skip_scenario_generates_business_hash_skip_sql(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd2_hash_skip_current_duplicate")
    output_dir = tmp_path / scenario.name

    generated_project = generate_project_for_scenario(scenario, output_dir)

    spec = load_yaml(generated_project.spec_path)
    model_sql = (output_dir / "models" / "generated" / "tms_int__account.sql").read_text(encoding="utf-8")
    assert spec["target"]["id"] == "tms_int__account"
    assert "name" not in spec["control_data"]["business_key"]
    assert spec["control_data"].get("skip_business_key") is not True
    assert spec["control_data"].get("skip_surrogate_key") is not True
    assert "cast(uuid_string() as varchar(36)) as ACCOUNT_KEY" in model_sql
    assert (
        "cast(sha2(concat_ws('|', coalesce(cast(cast(ACCOUNT_ID as varchar(20)) as varchar), '')), 256) "
        "as varchar) as ACCOUNT_BUSINESS_KEY"
        in model_sql
    )
    assert "existing_target.ACCOUNT_KEY as ACCOUNT_KEY" in model_sql
    assert "existing_target.ACCOUNT_BUSINESS_KEY as ACCOUNT_BUSINESS_KEY" in model_sql
    assert "unique_key=['ACCOUNT_BUSINESS_KEY']" in model_sql
    assert (
        "sha2(concat_ws('|', coalesce(cast(cast(ACCOUNT_ID as varchar(20)) as varchar), ''), "
        "coalesce(cast(cast(ACCOUNT_VALUE as number(10,0)) as varchar), '')), 256)"
        in model_sql
    )
    assert "cast('{{ var(\"insert_time\") }}' as timestamp_tz)" in model_sql
    assert "current_target.BUSINESS_DATA_HASH = typed_rows.BUSINESS_DATA_HASH" in model_sql
    assert "coalesce(current_target.IS_DELETED_FLAG, 'N') = typed_rows.TMS_IS_DELETED_FLAG_CANDIDATE" in model_sql
    assert "duplicate_boundary_rows as (" in model_sql
    assert "deduplicated_version_rows as (" in model_sql
    assert "SCD2 duplicate hash handling: historical duplicate boundaries updated=" in model_sql


def test_hash_exclude_scenario_omits_configured_field_from_business_hash(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd2_hash_exclude_source_field")
    output_dir = tmp_path / scenario.name

    generated_project = generate_project_for_scenario(scenario, output_dir)

    spec = load_yaml(generated_project.spec_path)
    model_sql = (output_dir / "models" / "generated" / "tms_int__account.sql").read_text(encoding="utf-8")
    hash_line = next(line for line in model_sql.splitlines() if "as BUSINESS_DATA_HASH" in line)
    assert spec["control_data"]["business_data_hash"] == {
        "business_data_hash_mode": "exclude",
        "fields": ["source_batch_id"],
    }
    assert "ACCOUNT_ID" in hash_line
    assert "ACCOUNT_VALUE" in hash_line
    assert "SOURCE_BATCH_ID" not in hash_line


def test_continuous_scd2_scenario_dbt_unit_test_uses_start_and_end_of_time(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd2_continuous_field_validity")
    output_dir = tmp_path / scenario.name

    generate_project_for_scenario(scenario, output_dir)

    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "tms_int__account_unit_tests.yml")
    expected_rows = unit_test_yaml["unit_tests"][0]["expect"]["rows"]
    assert expected_rows[0]["VALID_FROM_DATETIME"] == "0001-01-01T00:00:00Z"
    assert expected_rows[0]["VALID_TO_DATETIME"] == "9999-12-31T23:59:59Z"


def test_scd2_validation_failure_rollback_scenarios_generate_guards(tmp_path: Path) -> None:
    scenarios = {
        "scd2_validation_continuous_failure_rollback": True,
        "scd2_validation_continuous_gap_failure_rollback": True,
        "scd2_validation_sparse_failure_rollback": False,
    }
    for scenario_name, expects_continuity_check in scenarios.items():
        scenario = load_scenario(INTEGRATION_ROOT / scenario_name)
        output_dir = tmp_path / scenario.name

        generate_project_for_scenario(scenario, output_dir)

        model_sql = (output_dir / "models" / "generated" / "tms_int__account.sql").read_text(encoding="utf-8")
        assert scenario.loads[0].expect_dbt_success is False
        assert scenario.loads[0].expected_target_csv is not None
        assert "AUDIT_LAST_CHANGED_DATETIME" in scenario.expected_columns
        assert "unique_key=['ACCOUNT_BUSINESS_KEY']" in model_sql
        assert "VALID_TO_DATETIME <= VALID_FROM_DATETIME" in model_sql
        assert "TYPE_MATERIALISATION_SCD2_VALIDATION_FAILED" not in model_sql
        assert "cross join scd2_validation_guard" in model_sql
        assert "where scd2_validation_guard.SCD2_VALIDATION_GUARD = 0" in model_sql
        assert "post_load_validation_rows as (" in model_sql
        assert ("dateadd(nanosecond, 1, VALID_TO_DATETIME)" in model_sql) is expects_continuity_check
        assert "ACCOUNT_BUSINESS_KEY" in model_sql


def test_scd2_manual_failure_rollback_scenarios_generate_live_target_guards(tmp_path: Path) -> None:
    scenarios = [
        "scd2_manual_multiple_current_failure_rollback",
        "scd2_manual_overlap_failure_rollback",
    ]
    for scenario_name in scenarios:
        scenario = load_scenario(INTEGRATION_ROOT / scenario_name)
        output_dir = tmp_path / scenario.name

        generate_project_for_scenario(scenario, output_dir)

        guard_sql = (
            output_dir / "models" / "generated" / "tms_int__account__validation_guard.sql"
        ).read_text(encoding="utf-8")
        assert scenario.loads[0].expect_dbt_success is False
        assert scenario.loads[0].expected_target_csv is not None
        assert "AUDIT_LAST_CHANGED_DATETIME" in scenario.expected_columns
        assert (
            "adapter.get_relation(database=(target.database | upper), "
            "schema=(var(\"target_schema\", \"BUSINESS\") | upper), identifier='TMS_INT__ACCOUNT')"
            in guard_sql
        )
        assert "existing_manual_rows as (" in guard_sql
        assert "remaining_existing_manual_rows as (" in guard_sql
        assert "manual_candidate_rows as (" in guard_sql
        assert "manual_state_failures as (" in guard_sql
        assert "count_if(IS_CURRENT_FLAG = 'Y') over (partition by ACCOUNT_BUSINESS_KEY)" in guard_sql
        assert "TMS_NEXT_VALID_FROM_DATETIME <= VALID_TO_DATETIME" in guard_sql


def test_integration_row_comparison_normalises_case_order_and_whitespace(tmp_path: Path) -> None:
    csv_path = tmp_path / "expected.csv"
    csv_path.write_text("account_id,valid_to_datetime\nA1, 2026-07-05 00:00:00 \n", encoding="utf-8")

    expected = read_csv_rows(csv_path)
    actual = [{"ACCOUNT_ID": "A1", "VALID_TO_DATETIME": "2026-07-05 00:00:00+00:00"}]

    assert normalise_rows(actual) == normalise_rows(expected)


def test_integration_row_comparison_can_preserve_whitespace(tmp_path: Path) -> None:
    csv_path = tmp_path / "expected.csv"
    csv_path.write_text("account_id\nA1\n", encoding="utf-8")

    expected = read_csv_rows(csv_path)
    actual = [{"ACCOUNT_ID": " A1 "}]

    assert normalise_rows(actual) == normalise_rows(expected)
    assert normalise_rows(actual, preserve_whitespace=True) != normalise_rows(
        expected,
        preserve_whitespace=True,
    )
