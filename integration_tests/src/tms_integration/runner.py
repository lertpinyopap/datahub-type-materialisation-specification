from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from type_materialisation.dbt_generate import GenerateDbtOptions, generate_dbt_project

from .assertions import assert_rows_equal, read_csv_rows
from .database import (
    create_schema,
    create_target_table_from_spec,
    drop_relations,
    fetch_rows,
    insert_csv_rows,
    replace_source_table_from_csv,
    snowflake_connection,
)
from .dbt_runner import run_tms_dbt_project
from .reporting import IntegrationReporter
from .scenario import Scenario


@dataclass(frozen=True)
class GeneratedScenarioProject:
    project_dir: Path
    spec_path: Path


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
        _run_live_scenario(scenario, work_dir, reporter)
        return
    with IntegrationReporter() as owned_reporter:
        _run_live_scenario(scenario, work_dir, owned_reporter)


def _run_live_scenario(
    scenario: Scenario,
    work_dir: Path,
    reporter: IntegrationReporter,
) -> None:
    target_schema = _target_schema_name()
    table_prefix = _table_prefix()
    reporter.scenario_start(scenario, schema=target_schema, prefix=table_prefix)

    project_dir = work_dir / "dbt_project"
    reporter.step("Generating dbt project", str(project_dir))
    generated_project = generate_project_for_scenario(scenario, project_dir)
    reporter.ok("Generated dbt project", str(generated_project.project_dir))

    reporter.step("Connecting to Snowflake")
    with snowflake_connection() as connection:
        reporter.ok("Connected to Snowflake")
        reporter.step("Ensuring target schema exists", target_schema)
        create_schema(connection, target_schema)
        relation_names = _generated_relation_names(generated_project.spec_path)
        reporter.step("Pre-run cleanup of scenario relations", ", ".join(relation_names))
        drop_relations(connection, target_schema, relation_names)
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
                else:
                    reporter.step("Replacing generated seed data", step.source_csv.name)
                    _replace_seed(project_dir, step.source_csv)
                reporter.step("Running tms dbt-build")
                run_tms_dbt_project(
                    spec_path=generated_project.spec_path,
                    project_dir=project_dir,
                    target_schema=target_schema,
                )
                reporter.ok("dbt build completed")
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
        finally:
            if not _keep_tables_enabled():
                reporter.step("Final cleanup of scenario relations", ", ".join(relation_names))
                drop_relations(connection, target_schema, relation_names)
                reporter.ok("Cleanup completed")
            else:
                reporter.step("Keeping generated relations for inspection", ", ".join(relation_names))
    reporter.scenario_passed(scenario)


def _replace_seed(project_dir: Path, source_csv: Path) -> None:
    seed_dir = project_dir / "seeds"
    seed_files = list(seed_dir.glob("*.csv"))
    if len(seed_files) != 1:
        raise AssertionError(f"expected exactly one generated seed CSV in {seed_dir}, found {len(seed_files)}")
    shutil.copyfile(source_csv, seed_files[0])


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


def _source_table_name(spec_path: Path) -> str:
    spec = _load_spec(spec_path)
    source = spec.get("source", {})
    if not isinstance(source, dict) or source.get("format") != "table":
        raise AssertionError(f"scenario spec does not define a table source: {spec_path}")
    return str(source["table"]).upper()


def _generated_relation_names(spec_path: Path) -> list[str]:
    spec = _load_spec(spec_path)
    target_id = str(spec["target"]["id"])
    source = spec.get("source", {})
    seed = source.get("seed", {}) if isinstance(source, dict) else {}
    seed_name = seed.get("name", f"{target_id}__seed") if isinstance(seed, dict) else f"{target_id}__seed"
    control_data = spec.get("control_data", {})
    job = control_data.get("job", {}) if isinstance(control_data, dict) else {}
    job_table = job.get("table", "TYPE_MATERIALISATION_JOBS") if isinstance(job, dict) else "TYPE_MATERIALISATION_JOBS"
    relation_names = [
        str(spec["target"].get("table_name", target_id)),
        f"{target_id}__source",
        str(seed_name),
        f"{target_id}__quarantine",
        f"{target_id}__validation_guard",
        str(job_table),
    ]
    if isinstance(source, dict) and source.get("format") == "table":
        relation_names.append(str(source["table"]))
    return sorted({relation_name.upper() for relation_name in relation_names})


def _load_spec(spec_path: Path) -> dict:
    with spec_path.open("r", encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)
    if not isinstance(spec, dict):
        raise AssertionError(f"scenario spec is not a mapping: {spec_path}")
    return spec
