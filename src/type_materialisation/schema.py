import json
from pathlib import Path
from typing import Any

from .errors import DependencyError, Diagnostic

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schema"
CONCRETE_SCHEMA_PATH = SCHEMA_DIR / "type-materialisation.schema.json"
ABSTRACT_SCHEMA_PATH = SCHEMA_DIR / "type-materialisation-abstract.schema.json"


def require_yaml():
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise DependencyError(
            "PyYAML is required. Install dependencies with `pip install -r requirements.txt` "
            "or install the package with `pip install -e .`."
        ) from exc
    return yaml


def require_jsonschema():
    try:
        import jsonschema
    except ModuleNotFoundError as exc:
        raise DependencyError(
            "jsonschema is required. Install dependencies with `pip install -r requirements.txt` "
            "or install the package with `pip install -e .`."
        ) from exc
    return jsonschema


def load_yaml(path: Path) -> Any:
    yaml = require_yaml()
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return {} if data is None else data


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _validator(schema_path: Path):
    jsonschema = require_jsonschema()
    schema = load_json(schema_path)
    concrete_schema = load_json(CONCRETE_SCHEMA_PATH)
    store = {
        CONCRETE_SCHEMA_PATH.as_uri(): concrete_schema,
        concrete_schema.get("$id", ""): concrete_schema,
        "type-materialisation.schema.json": concrete_schema,
    }
    resolver = jsonschema.RefResolver(
        base_uri=schema_path.as_uri(),
        referrer=schema,
        store=store,
    )
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema, resolver=resolver)


def validate_schema(data: Any, *, abstract: bool) -> list[Diagnostic]:
    schema_path = ABSTRACT_SCHEMA_PATH if abstract else CONCRETE_SCHEMA_PATH
    validator = _validator(schema_path)
    diagnostics: list[Diagnostic] = []
    for error in sorted(validator.iter_errors(data), key=lambda item: list(item.path)):
        location = "$"
        if error.path:
            location = "$." + ".".join(str(part) for part in error.path)
        diagnostics.append(Diagnostic(error.message, location))
    return diagnostics
