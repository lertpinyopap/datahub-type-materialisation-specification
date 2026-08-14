from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from type_materialisation.schema import load_yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integration_tests" / "src"))

from tms_integration.assertions import normalise_rows, read_csv_rows  # noqa: E402
from tms_integration import dbt_runner  # noqa: E402
from tms_integration.runner import (  # noqa: E402
    _generated_relation_names,
    _keep_tables_enabled,
    _table_prefix,
    _target_schema_name,
    generate_project_for_scenario,
)
from tms_integration.scenario import discover_scenarios, load_scenario  # noqa: E402


INTEGRATION_ROOT = ROOT / "integration_tests"


def test_integration_scenarios_are_discoverable_and_self_contained() -> None:
    scenario_roots = discover_scenarios(INTEGRATION_ROOT)

    assert [path.name for path in scenario_roots] == [
        "scd2_continuous_field_validity",
        "scd2_missing_from_source_delete",
    ]
    for scenario_root in scenario_roots:
        scenario = load_scenario(scenario_root)
        assert scenario.readme.exists()
        assert scenario.spec_path.exists()
        assert scenario.dbt_unit_test_csv is not None
        assert scenario.dbt_unit_test_csv.exists()
        assert scenario.loads
        assert (scenario.root / "src").is_dir()
        for step in scenario.loads:
            assert step.source_csv.exists()
            assert step.expected_target_csv.exists()


def test_integration_scenarios_generate_dbt_unit_tests(tmp_path: Path) -> None:
    for scenario_root in discover_scenarios(INTEGRATION_ROOT):
        scenario = load_scenario(scenario_root)
        output_dir = tmp_path / scenario.name

        generated_project = generate_project_for_scenario(scenario, output_dir)

        unit_test_yaml = load_yaml(output_dir / "models" / "generated" / "tms_int__account_unit_tests.yml")
        assert generated_project.spec_path.name == f"{scenario.name}__prefixed_spec.yaml"
        assert unit_test_yaml["unit_tests"][0]["model"] == "tms_int__account"
        assert unit_test_yaml["unit_tests"][0]["given"][0]["input"] == "ref('tms_int__account__source')"
        assert unit_test_yaml["unit_tests"][0]["given"][0]["format"] == "sql"
        assert unit_test_yaml["unit_tests"][0]["expect"]["rows"]


def test_live_runner_defaults_to_tmp_schema(monkeypatch) -> None:
    monkeypatch.delenv("TMS_INTEGRATION_SCHEMA", raising=False)

    assert _target_schema_name() == "TMP"


def test_live_runner_defaults_to_integration_table_prefix(monkeypatch) -> None:
    monkeypatch.delenv("TMS_INTEGRATION_TABLE_PREFIX", raising=False)

    assert _table_prefix() == "TMS_INT__"


def test_live_runner_cleans_up_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TMS_INTEGRATION_KEEP_TABLES", raising=False)

    assert _keep_tables_enabled() is False


def test_live_runner_can_keep_tables_for_inspection(monkeypatch) -> None:
    monkeypatch.setenv("TMS_INTEGRATION_KEEP_TABLES", "1")

    assert _keep_tables_enabled() is True


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


def test_live_runner_identifies_generated_relations_for_cleanup(tmp_path: Path) -> None:
    scenario = load_scenario(INTEGRATION_ROOT / "scd2_missing_from_source_delete")
    output_dir = tmp_path / scenario.name

    generated_project = generate_project_for_scenario(scenario, output_dir)

    relation_names = _generated_relation_names(generated_project.spec_path)

    assert "TMS_INT__ACCOUNT" in relation_names
    assert "TMS_INT__ACCOUNT__SOURCE" in relation_names
    assert "TMS_INT__ACCOUNT_SOURCE_SEED" in relation_names
    assert "TMS_INT__TYPE_MATERIALISATION_JOBS" in relation_names


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
