import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import DependencyError, Diagnostic

# Keep REPO_ROOT for modules that still need the repository layout when running
# from a source checkout (for example custom Python macros under ./macros).
REPO_ROOT = Path(__file__).resolve().parents[2]

SCHEMA_PACKAGE = "type_materialisation"
SCHEMA_SOURCE_DIR = REPO_ROOT / "schema"
CONCRETE_SCHEMA_NAME = "type-materialisation.schema.json"
ABSTRACT_SCHEMA_NAME = "type-materialisation-abstract.schema.json"


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


def load_packaged_json(name: str) -> dict[str, Any]:
    packaged_path = files(SCHEMA_PACKAGE).joinpath(name)
    if packaged_path.is_file():
        return json.loads(packaged_path.read_text(encoding="utf-8"))

    # Source checkouts keep schema JSON files under the repo-root `schema/` folder
    # and stage them into the package only during wheel builds.
    return load_json(SCHEMA_SOURCE_DIR / name)


def _validator(*, abstract: bool):
    jsonschema = require_jsonschema()
    try:
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ModuleNotFoundError as exc:
        raise DependencyError(
            "referencing is required. Install dependencies with `pip install -r requirements.txt` "
            "or install the package with `pip install -e .`."
        ) from exc

    schema_name = ABSTRACT_SCHEMA_NAME if abstract else CONCRETE_SCHEMA_NAME
    schema = load_packaged_json(schema_name)
    concrete_schema = load_packaged_json(CONCRETE_SCHEMA_NAME)
    schema_resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    concrete_resource = Resource.from_contents(concrete_schema, default_specification=DRAFT202012)
    registry = Registry().with_resources(
        [
            (schema_name, schema_resource),
            (schema["$id"], schema_resource),
            (CONCRETE_SCHEMA_NAME, concrete_resource),
            (concrete_schema["$id"], concrete_resource),
            ("type-materialisation.schema.json", concrete_resource),
        ]
    )
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema, registry=registry)


def validate_schema(data: Any, *, abstract: bool) -> list[Diagnostic]:
    validator = _validator(abstract=abstract)
    diagnostics: list[Diagnostic] = []
    for error in sorted(validator.iter_errors(data), key=lambda item: list(item.path)):
        location = "$"
        if error.path:
            location = "$." + ".".join(str(part) for part in error.path)
        diagnostics.append(Diagnostic(error.message, location))
    return diagnostics
