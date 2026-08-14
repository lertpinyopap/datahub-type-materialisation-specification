from __future__ import annotations

import importlib.util
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from type_materialisation.dbt_generate import GenerateDbtOptions, generate_dbt_project

from .assertions import assert_rows_equal, read_csv_rows
from .database import (
    create_target_table_from_spec,
    drop_relations,
    drop_stages,
    fetch_relation_rows,
    fetch_rows,
    insert_csv_rows,
    replace_csv_stage_from_file,
    replace_source_table_from_csv,
    snowflake_connection,
)
from .dbt_runner import run_tms_dbt_project, validate_dbt_build_environment
from .reporting import IntegrationReporter
from .scenario import Scenario


@dataclass(frozen=True)
class GeneratedScenarioProject:
    project_dir: Path
    spec_path: Path


@dataclass(frozen=True)
class CustomAssertionContext:
    connection: Any
    scenario_name: str
    load_name: str
    target_schema: str
    table_prefix: str
    target_table: str
    project_dir: Path
    spec_path: Path
    source_csv: Path


def generate_project_for_scenario(scenario: Scenario, output_dir: Path) -> GeneratedScenarioProject:
    spec_path = _write_prefixed_spec_for_scenario(scenario, output_dir.parent)
    result = generate_dbt_project(
        GenerateDbtOptions(
            spec_path=spec_path,
            output_dir=output_dir,
            unit_test_csv=scenario.dbt_unit_test_csv,
        )
    )
    if result.errors:
        messages = "\n".join(f"{diagnostic.location}: {diagnostic.message}" for diagnostic in result.errors)
        raise AssertionError(f"dbt generation failed for {scenario.name}:\n{messages}")
    return GeneratedScenarioProject(project_dir=output_dir, spec_path=spec_path)


def run_live_scenario(
    scenario: Scenario,
    work_dir: Path,
    reporter: IntegrationReporter | None = None,
) -> None:
    if reporter is not None:
        _run_live_scenario_with_failure_reporting(scenario, work_dir, reporter)
        return
    with IntegrationReporter() as owned_reporter:
        _run_live_scenario_with_failure_reporting(scenario, work_dir, owned_reporter)


def _run_live_scenario_with_failure_reporting(
    scenario: Scenario,
    work_dir: Path,
    reporter: IntegrationReporter,
) -> None:
    try:
        _run_live_scenario(scenario, work_dir, reporter)
    except Exception as exc:
        reporter.scenario_failed(scenario, exc)
        raise


def _run_live_scenario(
    scenario: Scenario,
    work_dir: Path,
    reporter: IntegrationReporter,
) -> None:
    target_schema = _target_schema_name()
    table_prefix = _table_prefix()
    reporter.scenario_start(scenario, schema=target_schema, prefix=table_prefix)

    reporter.step("Checking local dbt command environment")
    validate_dbt_build_environment()
    reporter.ok("Local dbt command environment ready")

    project_dir = work_dir / "dbt_project"
    reporter.step("Generating dbt project", str(project_dir))
    generated_project = generate_project_for_scenario(scenario, project_dir)
    reporter.ok("Generated dbt project", str(generated_project.project_dir))
    custom_assertions = _load_custom_assertions(scenario.root)

    reporter.step("Connecting to Snowflake")
    with snowflake_connection() as connection:
        reporter.ok("Connected to Snowflake")
        reporter.step("Using existing target schema", target_schema)
        relation_names = _generated_relation_names(generated_project.spec_path)
        stage_names = _generated_stage_names(generated_project.spec_path)
        reporter.step("Pre-run cleanup of scenario relations", ", ".join(relation_names))
        drop_relations(connection, target_schema, relation_names)
        if stage_names:
            reporter.step("Pre-run cleanup of scenario stages", ", ".join(stage_names))
            drop_stages(connection, target_schema, stage_names)
        try:
            table = _target_table_name(generated_project.spec_path)
            if scenario.initial_target_csv is not None:
                reporter.step("Setting up initial target table", scenario.initial_target_csv.name)
                table = create_target_table_from_spec(connection, generated_project.spec_path, target_schema)
                insert_csv_rows(connection, target_schema, table, scenario.initial_target_csv, generated_project.spec_path)
                reporter.ok("Initial target data loaded", table)
            else:
                reporter.step("Starting without an existing target table", table)
            for step in scenario.loads:
                reporter.load_start(step)
                if _source_format(generated_project.spec_path) == "table":
                    source_table = _source_table_name(generated_project.spec_path)
                    reporter.step("Creating source table from VARCHAR CSV", source_table)
                    replace_source_table_from_csv(connection, target_schema, source_table, step.source_csv)
                    reporter.ok("Source table loaded", step.source_csv.name)
                elif _source_load_method(generated_project.spec_path) == "dbt_seed":
                    reporter.step("Replacing generated seed data", step.source_csv.name)
                    _replace_seed(project_dir, step.source_csv)
                else:
                    source_stage = _source_csv_stage_name(generated_project.spec_path)
                    reporter.step("Creating CSV stage and uploading file", source_stage)
                    replace_csv_stage_from_file(connection, target_schema, source_stage, step.source_csv)
                    reporter.ok("CSV stage loaded", step.source_csv.name)
                if step.force_runtime_failure:
                    reporter.step("Forcing generated model runtime failure", table)
                    _force_runtime_failure_model(project_dir, generated_project.spec_path)
                if step.dbt_vars:
                    reporter.step("Applying dbt vars", json.dumps(step.dbt_vars, sort_keys=True))
                reporter.step("Running tms dbt-build")
                dbt_result = run_tms_dbt_project(
                    spec_path=generated_project.spec_path,
                    project_dir=project_dir,
                    target_schema=target_schema,
                    dbt_vars=step.dbt_vars,
                    require_success=step.expect_dbt_success,
                )
                if step.expect_dbt_success:
                    reporter.ok("dbt build completed")
                elif dbt_result.returncode != 0:
                    reporter.ok("dbt build failed as expected", f"exit {dbt_result.returncode}")
                else:
                    raise AssertionError(f"expected dbt build to fail for load {step.name}")
                if step.expected_target_csv is not None:
                    expected_rows = read_csv_rows(step.expected_target_csv)
                    columns = scenario.expected_columns or list(expected_rows[0])
                    reporter.step("Reading target rows", table)
                    actual_rows = fetch_rows(
                        connection,
                        target_schema,
                        table,
                        columns,
                        generated_project.spec_path,
                        scenario.order_by,
                    )
                    reporter.checks(scenario.checks)
                    assert_rows_equal(
                        actual_rows,
                        expected_rows,
                        preserve_whitespace=scenario.preserve_whitespace,
                    )
                    reporter.ok("Expected target rows matched", f"{len(actual_rows)} rows")
                for expected_relation in step.expected_relations:
                    relation_table = _prefixed_logical_name(expected_relation.table).upper()
                    relation_expected_rows = read_csv_rows(expected_relation.expected_csv)
                    reporter.step("Reading expected relation", f"{expected_relation.name}: {relation_table}")
                    relation_actual_rows = fetch_relation_rows(
                        connection,
                        target_schema,
                        relation_table,
                        expected_relation.columns,
                        expected_relation.order_by,
                    )
                    assert_rows_equal(
                        relation_actual_rows,
                        relation_expected_rows,
                        preserve_whitespace=expected_relation.preserve_whitespace,
                    )
                    reporter.ok(
                        "Expected relation rows matched",
                        f"{expected_relation.name}: {len(relation_actual_rows)} rows",
                    )
                if custom_assertions is not None:
                    reporter.step("Running custom src assertion", step.name)
                    custom_assertions.assert_after_load(
                        CustomAssertionContext(
                            connection=connection,
                            scenario_name=scenario.name,
                            load_name=step.name,
                            target_schema=target_schema,
                            table_prefix=table_prefix,
                            target_table=table,
                            project_dir=project_dir,
                            spec_path=generated_project.spec_path,
                            source_csv=step.source_csv,
                        )
                    )
                    reporter.ok("Custom src assertion passed", step.name)
        finally:
            if not _keep_tables_enabled():
                reporter.step("Final cleanup of scenario relations", ", ".join(relation_names))
                drop_relations(connection, target_schema, relation_names)
                if stage_names:
                    reporter.step("Final cleanup of scenario stages", ", ".join(stage_names))
                    drop_stages(connection, target_schema, stage_names)
                reporter.ok("Cleanup completed")
            else:
                kept_names = [*relation_names, *stage_names]
                reporter.step("Keeping generated relations for inspection", ", ".join(kept_names))
    reporter.scenario_passed(scenario)


def _replace_seed(project_dir: Path, source_csv: Path) -> None:
    seed_dir = project_dir / "seeds"
    seed_files = list(seed_dir.glob("*.csv"))
    if len(seed_files) != 1:
        raise AssertionError(f"expected exactly one generated seed CSV in {seed_dir}, found {len(seed_files)}")
    shutil.copyfile(source_csv, seed_files[0])


def _force_runtime_failure_model(project_dir: Path, spec_path: Path) -> None:
    spec = _load_spec(spec_path)
    target_id = str(spec["target"]["id"])
    model_path = project_dir / "models" / "generated" / f"{target_id}.sql"
    if not model_path.is_file():
        raise AssertionError(f"generated final model does not exist: {model_path}")
    model_path.write_text(
        "\n".join(
            [
                "{{ config(materialized='table') }}",
                "",
                "select *",
                "from TMS_INT__RELATION_THAT_DOES_NOT_EXIST_FOR_RUNTIME_FAILURE",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _load_custom_assertions(scenario_root: Path) -> Any | None:
    assertions_path = scenario_root / "src" / "assertions.py"
    if not assertions_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        f"tms_integration_assertions_{scenario_root.name}",
        assertions_path,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load custom assertions module: {assertions_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert_after_load = getattr(module, "assert_after_load", None)
    if not callable(assert_after_load):
        raise AssertionError(f"{assertions_path} must define callable assert_after_load(context)")
    return module


def _target_schema_name() -> str:
    return os.environ.get("TMS_INTEGRATION_SCHEMA", "TMP").upper()


def _table_prefix() -> str:
    prefix = os.environ.get("TMS_INTEGRATION_TABLE_PREFIX", "TMS_INT__").upper()
    if len(prefix) < 3 or not prefix.endswith("__"):
        raise AssertionError(
            "TMS_INTEGRATION_TABLE_PREFIX must be at least 3 characters long and end with `__`"
        )
    return prefix


def _keep_tables_enabled() -> bool:
    return os.environ.get("TMS_INTEGRATION_KEEP_TABLES") == "1"


def _logical_table_prefix() -> str:
    return _table_prefix().lower()


def _write_prefixed_spec_for_scenario(scenario: Scenario, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    spec = _load_spec(scenario.spec_path)
    _prefix_spec_relations(spec, scenario.spec_path)
    prefixed_spec_path = output_dir / f"{scenario.name}__prefixed_spec.yaml"
    prefixed_spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return prefixed_spec_path


def _prefix_spec_relations(spec: dict, original_spec_path: Path) -> None:
    target = spec["target"]
    original_target_id = str(target["id"])
    target["id"] = _prefixed_logical_name(original_target_id)
    target["table_name"] = _prefixed_logical_name(str(target.get("table_name", original_target_id)))

    source = spec.get("source", {})
    if isinstance(source, dict) and source.get("format") == "csv" and source.get("load_method") == "dbt_seed":
        seed = source.setdefault("seed", {})
        if not isinstance(seed, dict):
            seed = {}
            source["seed"] = seed
        seed["name"] = _prefixed_logical_name(str(seed.get("name", f"{original_target_id}__seed")))
        if isinstance(seed.get("file"), str):
            seed_file = Path(seed["file"])
            if not seed_file.is_absolute():
                seed_file = original_spec_path.parent / seed_file
            seed["file"] = str(seed_file)
    elif isinstance(source, dict) and source.get("format") == "table":
        source["schema"] = "{{ var('target_schema', 'TMP') }}"
        source["table"] = _prefixed_logical_name(str(source["table"]))
    elif isinstance(source, dict) and source.get("format") == "csv":
        location = source.setdefault("location", {})
        if not isinstance(location, dict):
            location = {}
            source["location"] = location
        location["schema"] = "{{ var('target_schema', 'TMP') }}"
        location["stage"] = _prefixed_logical_name(_stage_name_from_location(location.get("stage", "csv_stage")))

    control_data = spec.setdefault("control_data", {})
    if isinstance(control_data, dict):
        job = control_data.setdefault("job", {})
        if isinstance(job, dict):
            job["table"] = _prefixed_logical_name(str(job.get("table", "TYPE_MATERIALISATION_JOBS")))
        quarantine = control_data.get("quarantine")
        if isinstance(quarantine, dict) and isinstance(quarantine.get("table"), str):
            quarantine["table"] = _prefixed_logical_name(quarantine["table"])


def _prefixed_logical_name(name: str) -> str:
    prefix = _logical_table_prefix()
    return name if name.lower().startswith(prefix) else f"{prefix}{name}"


def _target_table_name(spec_path: Path) -> str:
    spec = _load_spec(spec_path)
    return str(spec["target"].get("table_name", spec["target"]["id"])).upper()


def _source_format(spec_path: Path) -> str:
    spec = _load_spec(spec_path)
    source = spec.get("source", {})
    return str(source.get("format", "")) if isinstance(source, dict) else ""


def _source_load_method(spec_path: Path) -> str:
    spec = _load_spec(spec_path)
    source = spec.get("source", {})
    return str(source.get("load_method", "stage")) if isinstance(source, dict) else ""


def _source_table_name(spec_path: Path) -> str:
    spec = _load_spec(spec_path)
    source = spec.get("source", {})
    if not isinstance(source, dict) or source.get("format") != "table":
        raise AssertionError(f"scenario spec does not define a table source: {spec_path}")
    return str(source["table"]).upper()


def _source_csv_stage_name(spec_path: Path) -> str:
    spec = _load_spec(spec_path)
    source = spec.get("source", {})
    location = source.get("location", {}) if isinstance(source, dict) else {}
    if not isinstance(location, dict):
        location = {}
    return _stage_name_from_location(location.get("stage", "csv_stage")).upper()


def _generated_relation_names(spec_path: Path) -> list[str]:
    spec = _load_spec(spec_path)
    target_id = str(spec["target"]["id"])
    target_table = str(spec["target"].get("table_name", target_id))
    source = spec.get("source", {})
    seed = source.get("seed", {}) if isinstance(source, dict) else {}
    seed_name = seed.get("name", f"{target_id}__seed") if isinstance(seed, dict) else f"{target_id}__seed"
    control_data = spec.get("control_data", {})
    job = control_data.get("job", {}) if isinstance(control_data, dict) else {}
    job_table = job.get("table", "TYPE_MATERIALISATION_JOBS") if isinstance(job, dict) else "TYPE_MATERIALISATION_JOBS"
    quarantine = control_data.get("quarantine", {}) if isinstance(control_data, dict) else {}
    quarantine_table = (
        quarantine.get("table", f"{target_table}__quarantine")
        if isinstance(quarantine, dict)
        else f"{target_table}__quarantine"
    )
    relation_names = [
        target_table,
        f"{target_id}__source",
        str(seed_name),
        f"{target_id}__quarantine",
        f"{target_id}__validation_guard",
        str(quarantine_table),
        str(job_table),
    ]
    if isinstance(source, dict) and source.get("format") == "table":
        relation_names.append(str(source["table"]))
    return sorted({relation_name.upper() for relation_name in relation_names})


def _generated_stage_names(spec_path: Path) -> list[str]:
    spec = _load_spec(spec_path)
    source = spec.get("source", {})
    if not isinstance(source, dict) or source.get("format") != "csv" or source.get("load_method", "stage") == "dbt_seed":
        return []
    location = source.get("location", {})
    if not isinstance(location, dict):
        location = {}
    return [_stage_name_from_location(location.get("stage", "csv_stage")).upper()]


def _stage_name_from_location(value: object) -> str:
    text = str(value)
    if text.startswith("@"):
        text = text[1:]
    text = text.split("/", 1)[0]
    return text.rsplit(".", 1)[-1]


def _load_spec(spec_path: Path) -> dict:
    with spec_path.open("r", encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)
    if not isinstance(spec, dict):
        raise AssertionError(f"scenario spec is not a mapping: {spec_path}")
    return spec
