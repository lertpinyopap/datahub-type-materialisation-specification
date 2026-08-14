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
    snowflake_connection,
)
from .dbt_runner import run_tms_dbt_project
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


def run_live_scenario(scenario: Scenario, work_dir: Path) -> None:
    target_schema = _target_schema_name()
    project_dir = work_dir / "dbt_project"
    generated_project = generate_project_for_scenario(scenario, project_dir)
    with snowflake_connection() as connection:
        create_schema(connection, target_schema)
        relation_names = _generated_relation_names(generated_project.spec_path)
        drop_relations(connection, target_schema, relation_names)
        try:
            table = _target_table_name(generated_project.spec_path)
            if scenario.initial_target_csv is not None:
                table = create_target_table_from_spec(connection, generated_project.spec_path, target_schema)
                insert_csv_rows(connection, target_schema, table, scenario.initial_target_csv, generated_project.spec_path)
            for step in scenario.loads:
                _replace_seed(project_dir, step.source_csv)
                run_tms_dbt_project(
                    spec_path=generated_project.spec_path,
                    project_dir=project_dir,
                    target_schema=target_schema,
                )
                expected_rows = read_csv_rows(step.expected_target_csv)
                columns = scenario.expected_columns or list(expected_rows[0])
                actual_rows = fetch_rows(
                    connection,
                    target_schema,
                    table,
                    columns,
                    generated_project.spec_path,
                    scenario.order_by,
                )
                assert_rows_equal(actual_rows, expected_rows)
        finally:
            if not _keep_tables_enabled():
                drop_relations(connection, target_schema, relation_names)


def _replace_seed(project_dir: Path, source_csv: Path) -> None:
    seed_dir = project_dir / "seeds"
    seed_files = list(seed_dir.glob("*.csv"))
    if len(seed_files) != 1:
        raise AssertionError(f"expected exactly one generated seed CSV in {seed_dir}, found {len(seed_files)}")
    shutil.copyfile(source_csv, seed_files[0])


def _target_schema_name() -> str:
    return os.environ.get("TMS_INTEGRATION_SCHEMA", "TMP").upper()


def _table_prefix() -> str:
    return os.environ.get("TMS_INTEGRATION_TABLE_PREFIX", "TMS_INT__").upper()


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
    return sorted({relation_name.upper() for relation_name in relation_names})


def _load_spec(spec_path: Path) -> dict:
    with spec_path.open("r", encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)
    if not isinstance(spec, dict):
        raise AssertionError(f"scenario spec is not a mapping: {spec_path}")
    return spec
