from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ScenarioError(ValueError):
    """Raised when a scenario folder is malformed."""


@dataclass(frozen=True)
class ExpectedRelation:
    name: str
    table: str
    expected_csv: Path
    columns: list[str]
    order_by: list[str]
    preserve_whitespace: bool


@dataclass(frozen=True)
class LoadStep:
    name: str
    source_csv: Path | None
    source_json: Path | None
    expected_target_csv: Path | None
    dbt_vars: dict[str, Any]
    expect_dbt_success: bool
    force_runtime_failure: bool
    expected_relations: list[ExpectedRelation]


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    root: Path
    readme: Path
    spec_path: Path
    dbt_unit_test_csv: Path | None
    initial_target_csv: Path | None
    expected_columns: list[str]
    order_by: list[str]
    preserve_whitespace: bool
    checks: list[str]
    loads: list[LoadStep]


def discover_scenarios(base_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in base_dir.iterdir()
        if path.is_dir() and (path / "scenario.yaml").exists()
    )


def load_scenario(root: Path) -> Scenario:
    manifest_path = root / "scenario.yaml"
    readme = _required_file(root, "README.md")
    manifest = _load_yaml_mapping(manifest_path)
    name = _required_string(manifest, "name", manifest_path)
    description = _optional_string(manifest.get("description"))
    spec_path = _required_file(root, _required_string(manifest, "spec", manifest_path))

    dbt_config = manifest.get("dbt", {})
    if dbt_config is None:
        dbt_config = {}
    if not isinstance(dbt_config, dict):
        raise ScenarioError(f"{manifest_path}: `dbt` must be a mapping")
    dbt_unit_test_csv = _optional_file(root, dbt_config.get("unit_test_csv"))

    target_config = manifest.get("target", {})
    if not isinstance(target_config, dict):
        raise ScenarioError(f"{manifest_path}: `target` must be a mapping")
    expected_columns = [str(column).upper() for column in target_config.get("expected_columns", [])]
    order_by = [str(column).upper() for column in target_config.get("order_by", [])]
    preserve_whitespace = target_config.get("preserve_whitespace") is True
    checks = _optional_string_list(manifest.get("checks"), manifest_path, "checks")

    initial_target_csv = _optional_file(root, manifest.get("initial_target"))
    loads = _load_steps(root, manifest, manifest_path)
    return Scenario(
        name=name,
        description=description,
        root=root,
        readme=readme,
        spec_path=spec_path,
        dbt_unit_test_csv=dbt_unit_test_csv,
        initial_target_csv=initial_target_csv,
        expected_columns=expected_columns,
        order_by=order_by,
        preserve_whitespace=preserve_whitespace,
        checks=checks,
        loads=loads,
    )


def _load_steps(root: Path, manifest: dict[str, Any], manifest_path: Path) -> list[LoadStep]:
    raw_loads = manifest.get("loads")
    if not isinstance(raw_loads, list) or not raw_loads:
        raise ScenarioError(f"{manifest_path}: `loads` must contain at least one load step")
    steps: list[LoadStep] = []
    for index, raw_step in enumerate(raw_loads):
        if not isinstance(raw_step, dict):
            raise ScenarioError(f"{manifest_path}: load step {index} must be a mapping")
        name = _required_string(raw_step, "name", manifest_path)
        source_csv = _optional_file(root, raw_step.get("source_csv"))
        source_json = _optional_file(root, raw_step.get("source_json"))
        if (source_csv is None) == (source_json is None):
            raise ScenarioError(f"{manifest_path}: load step {index} must specify exactly one of `source_csv` or `source_json`")
        expected_target_csv = _optional_file(root, raw_step.get("expected_target"))
        dbt_vars = _optional_mapping(raw_step.get("dbt_vars"), manifest_path, f"loads[{index}].dbt_vars")
        expect_dbt_success = raw_step.get("expect_dbt_success", True) is not False
        force_runtime_failure = raw_step.get("force_runtime_failure") is True
        if expected_target_csv is None and expect_dbt_success:
            raise ScenarioError(f"{manifest_path}: load step {index} requires `expected_target`")
        expected_relations = _load_expected_relations(
            root,
            raw_step.get("expected_relations"),
            manifest_path,
            f"loads[{index}].expected_relations",
        )
        steps.append(
            LoadStep(
                name=name,
                source_csv=source_csv,
                source_json=source_json,
                expected_target_csv=expected_target_csv,
                dbt_vars=dbt_vars,
                expect_dbt_success=expect_dbt_success,
                force_runtime_failure=force_runtime_failure,
                expected_relations=expected_relations,
            )
        )
    return steps


def _load_expected_relations(
    root: Path,
    raw_relations: Any,
    manifest_path: Path,
    key: str,
) -> list[ExpectedRelation]:
    if raw_relations is None:
        return []
    if not isinstance(raw_relations, list):
        raise ScenarioError(f"{manifest_path}: `{key}` must be a list")
    relations: list[ExpectedRelation] = []
    for index, raw_relation in enumerate(raw_relations):
        relation_key = f"{key}[{index}]"
        if not isinstance(raw_relation, dict):
            raise ScenarioError(f"{manifest_path}: `{relation_key}` must be a mapping")
        name = _required_string(raw_relation, "name", manifest_path)
        table = _required_string(raw_relation, "table", manifest_path)
        expected_csv = _required_file(root, _required_string(raw_relation, "expected_csv", manifest_path))
        columns = [
            str(column).upper()
            for column in _required_string_list(raw_relation, manifest_path, relation_key, "columns")
        ]
        order_by = [
            str(column).upper()
            for column in _optional_string_list(
                raw_relation.get("order_by"),
                manifest_path,
                f"{relation_key}.order_by",
            )
        ]
        preserve_whitespace = raw_relation.get("preserve_whitespace") is True
        relations.append(
            ExpectedRelation(
                name=name,
                table=table,
                expected_csv=expected_csv,
                columns=columns,
                order_by=order_by,
                preserve_whitespace=preserve_whitespace,
            )
        )
    return relations


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ScenarioError(f"{path}: scenario manifest must be a mapping")
    return data


def _required_string(mapping: dict[str, Any], key: str, path: Path) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"{path}: `{key}` is required")
    return value


def _optional_string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_string_list(value: Any, path: Path, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ScenarioError(f"{path}: `{key}` must be a list of non-empty strings")
    return value


def _required_string_list(mapping: dict[str, Any], path: Path, parent_key: str, key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ScenarioError(f"{path}: `{parent_key}.{key}` must be a non-empty list of strings")
    return value


def _optional_mapping(value: Any, path: Path, key: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScenarioError(f"{path}: `{key}` must be a mapping")
    return {str(mapping_key): mapping_value for mapping_key, mapping_value in value.items()}


def _required_file(root: Path, relative_path: str) -> Path:
    path = root / relative_path
    if not path.is_file():
        raise ScenarioError(f"{root / 'scenario.yaml'}: required file does not exist: {relative_path}")
    return path


def _optional_file(root: Path, value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"{root / 'scenario.yaml'}: optional file path must be a string")
    return _required_file(root, value)
