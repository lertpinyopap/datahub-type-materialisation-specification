from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

from rich.console import Console

from type_materialisation.schema import load_yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integration_tests" / "src"))

from tms_integration.assertions import normalise_rows, read_csv_rows  # noqa: E402
from tms_integration import dbt_runner  # noqa: E402
from tms_integration import database as integration_database  # noqa: E402
from tms_integration import reporting as integration_reporting  # noqa: E402
from tms_integration.database import (  # noqa: E402
    DEFAULT_SNOWFLAKE_CONNECTION,
    IntegrationConfigError,
    drop_relations,
    insert_csv_rows,
    load_snowflake_connection_config,
    replace_source_table_from_csv,
)
from tms_integration.runner import (  # noqa: E402
    _generated_relation_names,
    _keep_tables_enabled,
    _source_format,
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

    def execute(self, sql, values=None):
        self.executed.append((sql, values))

    def executemany(self, sql, values):
        del sql, values
        self.executemany_called = True

    def close(self):
        self.closed = True


def test_integration_scenarios_are_discoverable_and_self_contained() -> None:
    scenario_roots = discover_scenarios(INTEGRATION_ROOT)

    assert [path.name for path in scenario_roots] == [
        "scd1_csv_transforms",
        "scd1_table_source_varchar_load",
        "scd2_continuous_field_validity",
        "scd2_missing_from_source_delete",
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
            assert step.source_csv.exists()
            assert step.expected_target_csv.exists()


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
            assert unit_test_yaml["unit_tests"][0]["overrides"] == {"macros": {"is_incremental": False}}
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
    assert text.startswith("\n\n")
    assert "scd2_missing_from_source_delete" in text
    assert "Setting up initial target table" in text
    assert "The previous active A1 row is end dated at the delete valid_from_datetime." in text
    assert "Expected target rows matched" in text


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


def test_integration_dbt_runner_uses_tms_dbt_build(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    spec_path = tmp_path / "spec.yaml"
    project_dir = tmp_path / "project"
    spec_path.write_text("id: account\n", encoding="utf-8")

    monkeypatch.setattr(dbt_runner.shutil, "which", lambda executable: "/venv/bin/tms" if executable == "tms" else None)

    def fake_run(command, *, check, capture_output, text):
        commands.append(command)
        assert check is False
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(dbt_runner.subprocess, "run", fake_run)

    dbt_runner.run_tms_dbt_project(spec_path=spec_path, project_dir=project_dir, target_schema="TMP")

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
            json.dumps({"target_schema": "TMP", "tms_job_schema": "TMP"}),
        ]
    ]


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
    assert "from {{ var('target_schema', 'TMP') }}.TMS_INT__ACCOUNT_SOURCE_TABLE" in source_sql


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


def test_continuous_scd2_scenario_dbt_unit_test_uses_exact_next_boundary(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd2_continuous_field_validity")
    output_dir = tmp_path / scenario.name

    generate_project_for_scenario(scenario, output_dir)

    unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "tms_int__account_unit_tests.yml")
    expected_rows = unit_test_yaml["unit_tests"][0]["expect"]["rows"]
    assert expected_rows[0]["VALID_TO_DATETIME"] == "2026-07-05T00:00:00Z"
    assert expected_rows[1]["VALID_FROM_DATETIME"] == "2026-07-05T00:00:00Z"


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
