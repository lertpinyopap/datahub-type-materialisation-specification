from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ScenarioError(ValueError):
    """Raised when a scenario folder is malformed."""


@dataclass(frozen=True)
class LoadStep:
    name: str
    source_csv: Path
    expected_target_csv: Path
    dbt_vars: dict[str, Any]


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
        source_csv = _required_file(root, _required_string(raw_step, "source_csv", manifest_path))
        expected_target_csv = _required_file(root, _required_string(raw_step, "expected_target", manifest_path))
        dbt_vars = _optional_mapping(raw_step.get("dbt_vars"), manifest_path, f"loads[{index}].dbt_vars")
        steps.append(
            LoadStep(
                name=name,
                source_csv=source_csv,
                expected_target_csv=expected_target_csv,
                dbt_vars=dbt_vars,
            )
        )
    return steps


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
