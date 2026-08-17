import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .errors import Diagnostic
from .schema import require_yaml

VARIABLE_EXPR_RE = re.compile(
    r"""{{\s*tms_var\(\s*(['"])([^'"]+)\1\s*(?:,\s*(['"])([^'"]*)\3\s*)?\)\s*}}"""
)


def parse_vars(raw_vars: str | None) -> tuple[dict[str, Any], list[Diagnostic]]:
    if raw_vars is None:
        return {}, []
    yaml = require_yaml()
    loaded = yaml.safe_load(raw_vars)
    if loaded is None:
        return {}, []
    if not isinstance(loaded, dict):
        return {}, [Diagnostic("vars must be a YAML/JSON mapping", "--vars")]
    return {str(key): value for key, value in loaded.items()}, []


def resolve_python_template(
    value: str,
    *,
    variables: Mapping[str, Any] | None = None,
) -> str:
    variables = variables or {}

    def replace(match: re.Match[str]) -> str:
        variable_name = match.group(2)
        default = match.group(4)
        if variable_name in variables:
            return str(variables[variable_name])
        if default is not None:
            return default
        raise ValueError(f"variable `{variable_name}` was not provided")

    return VARIABLE_EXPR_RE.sub(replace, value)


def resolve_python_templates(
    value: Any,
    *,
    variables: Mapping[str, Any] | None = None,
) -> tuple[Any, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    resolved = _resolve_value(deepcopy(value), "$", variables=variables, diagnostics=diagnostics)
    return resolved, diagnostics


def _resolve_value(
    value: Any,
    path: str,
    *,
    variables: Mapping[str, Any] | None,
    diagnostics: list[Diagnostic],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_value(child, f"{path}.{key}", variables=variables, diagnostics=diagnostics)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_value(child, f"{path}[{index}]", variables=variables, diagnostics=diagnostics)
            for index, child in enumerate(value)
        ]
    if isinstance(value, str) and "{{" in value:
        try:
            return resolve_python_template(value, variables=variables)
        except ValueError as exc:
            diagnostics.append(Diagnostic(str(exc), path))
            return value
    return value
