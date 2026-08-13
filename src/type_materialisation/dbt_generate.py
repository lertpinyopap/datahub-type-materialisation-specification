import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .custom_macros import MacroLoadError, PythonMacroResolver
from .errors import Diagnostic
from .schema import load_yaml, require_yaml
from .spec import fields


NOT_IMPLEMENTED = [
    "inheritance resolution for dbt generation",
    "table source dbt generation",
    "SCD1 and SCD2 materialisation",
    "job event table writes",
    "quarantine table writes",
    "dbt enforcement of validation rules and failure_mode",
    "uniqueness checks in generated dbt SQL",
    "generated Snowflake file-format objects for CSV stages",
    "Python upload of local CSV files to Snowflake stages",
    "date/timestamp format translation from Python strptime to Snowflake formats",
    "custom Python execution inside dbt macros",
    "database-native Python UDF generation",
]


@dataclass
class DbtGenerationResult:
    output_dir: Path
    files: list[Path] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    errors: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class GenerateDbtOptions:
    spec_path: Path
    output_dir: Path
    csv_stage: str | None = None
    unit_test_csv: Path | None = None
    macro_paths: list[Path] = field(default_factory=list)


def generate_dbt_project(options: GenerateDbtOptions) -> DbtGenerationResult:
    spec = load_yaml(options.spec_path)
    result = DbtGenerationResult(output_dir=options.output_dir)
    diagnostics = _unsupported_for_initial_dbt_generation(spec)
    if diagnostics:
        result.errors.extend(diagnostics)
        return result

    macros = PythonMacroResolver(spec_path=options.spec_path, macro_paths=options.macro_paths)
    custom_macro_names = _custom_macro_names(spec)
    generated_macros: dict[str, str] = {}
    for macro_name in custom_macro_names:
        try:
            macros.require_sql_generation(macro_name)
            reference = macros.resolve_reference(macro_name)
            generated_macros[reference.macro_name] = reference.macro.generate_dbt_macro()
            if not macros.has_python_callable(macro_name):
                result.warnings.append(
                    Diagnostic(
                        "custom macro has no Python execution callable; generated dbt SQL will be used at runtime",
                        macro_name,
                    )
                )
        except MacroLoadError as exc:
            result.errors.append(Diagnostic(str(exc), macro_name))
    if result.errors:
        return result

    _ensure_dirs(options.output_dir)
    _write_project_file(options.output_dir, result)
    _write_status_file(options.output_dir, result)
    _write_source_model(spec, options, result)
    try:
        _write_final_model(spec, result)
    except ValueError as exc:
        result.errors.append(Diagnostic(str(exc), "$.target.fields"))
        return result
    _write_generated_macros(options.output_dir, generated_macros, result)
    if options.unit_test_csv is not None:
        _write_unit_tests(spec, options, macros, result)
    result.warnings = _dedupe_diagnostics(result.warnings)
    return result


def _unsupported_for_initial_dbt_generation(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    source = spec.get("source", {})
    control_data = spec.get("control_data", {})
    if not isinstance(source, dict) or source.get("format") != "csv":
        diagnostics.append(Diagnostic("initial dbt generation supports only source.format = csv", "$.source.format"))
    if isinstance(control_data, dict) and control_data.get("change_type") in {"scd1", "scd2"}:
        diagnostics.append(Diagnostic("SCD materialisation is not implemented yet", "$.control_data.change_type"))
    return diagnostics


def _ensure_dirs(output_dir: Path) -> None:
    (output_dir / "models" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "reference").mkdir(parents=True, exist_ok=True)


def _write_project_file(output_dir: Path, result: DbtGenerationResult) -> None:
    content = """name: type_materialisation_generated
version: "1.0"
config-version: 2
profile: type_materialisation_generated

model-paths: ["models"]
macro-paths: ["macros"]

models:
  type_materialisation_generated:
    generated:
      +materialized: view
"""
    _write(output_dir / "dbt_project.yml", content, result)


def _write_status_file(output_dir: Path, result: DbtGenerationResult) -> None:
    lines = [
        "# Not Yet Implemented",
        "",
        "This generated dbt project covers the first reference implementation slice only.",
        "",
    ]
    lines.extend(f"- {item}" for item in NOT_IMPLEMENTED)
    _write(output_dir / "NOT_IMPLEMENTED.md", "\n".join(lines) + "\n", result)


def _write_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    target = spec["target"]
    model_name = _source_model_name(target["id"])
    stage = _stage_reference(options.csv_stage) if options.csv_stage else _csv_stage_location(spec["source"])
    select_lines = []
    for field in fields(spec):
        source = field.get("source", {})
        pos = source.get("pos")
        column = source.get("column", field["id"])
        ordinal = int(pos) + 1
        select_lines.append(f"    ${ordinal}::string as {_quote_identifier(column)}")
    sql = "\n".join(
        [
            f"{{{{ config(materialized='view', alias='{model_name}') }}}}",
            "",
            "select",
            ",\n".join(select_lines),
            f"from {stage}",
            "",
        ]
    )
    _write(options.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _write_final_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    table_name = target.get("table_name", target["id"])
    materialized = spec.get("control_data", {}).get("materialisation_type", "view")
    source_model = _source_model_name(target["id"])
    select_lines = []
    for field in fields(spec):
        expression = _field_expression(field)
        data_type = field["data_type"]
        select_lines.append(f"    cast({expression} as {data_type}) as {_quote_identifier(field['id'])}")
    select_lines.extend(
        [
            "    cast('{{ var(\"audit_data_process_key\", \"manual\") }}' as varchar) as audit_data_process_key",
            "    current_timestamp() as audit_created_datetime",
            "    current_timestamp() as audit_last_changed_datetime",
        ]
    )
    config_lines = [
        f"    materialized='{materialized}',",
        f"    schema='{target['schema']}',",
        f"    alias='{table_name}'",
    ]
    if target.get("database"):
        config_lines.insert(1, f"    database='{target['database']}',")
    sql = "\n".join(
        ["{{", "  config(", *config_lines, "  )", "}}", "", "select", ",\n".join(select_lines), f"from {{{{ ref('{source_model}') }}}}", ""]
    )
    _write(result.output_dir / "models" / "generated" / f"{target['id']}.sql", sql, result)


def _write_generated_macros(output_dir: Path, macros: dict[str, str], result: DbtGenerationResult) -> None:
    for macro_name, macro_sql in sorted(macros.items()):
        _write(output_dir / "macros" / "generated" / f"{macro_name}.sql", macro_sql + "\n", result)


def _write_unit_tests(
    spec: dict[str, Any],
    options: GenerateDbtOptions,
    macros: PythonMacroResolver,
    result: DbtGenerationResult,
) -> None:
    yaml = require_yaml()
    source_rows, expected_rows, warnings = _unit_test_rows(spec, options.unit_test_csv, macros)
    result.warnings.extend(warnings)
    target = spec["target"]
    data = {
        "unit_tests": [
            {
                "name": f"{target['id']}_sample_csv",
                "model": target["id"],
                "given": [
                    {
                        "input": f"ref('{_source_model_name(target['id'])}')",
                        "rows": source_rows,
                    }
                ],
                "expect": {
                    "rows": expected_rows,
                },
            }
        ]
    }
    content = yaml.safe_dump(data, sort_keys=False)
    _write(result.output_dir / "models" / "generated" / f"{target['id']}_unit_tests.yml", content, result)


def _unit_test_rows(
    spec: dict[str, Any],
    csv_path: Path,
    macros: PythonMacroResolver,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Diagnostic]]:
    warnings: list[Diagnostic] = []
    source = spec["source"]
    target_fields = fields(spec)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=source.get("separator", ","), quotechar=source.get("quote_char", '"'))
        header = next(reader) if source.get("header") else None
        source_rows: list[dict[str, Any]] = []
        expected_rows: list[dict[str, Any]] = []
        for row in reader:
            source_row: dict[str, Any] = {}
            expected_row: dict[str, Any] = {}
            for field in target_fields:
                column = field.get("source", {}).get("column", field["id"])
                value = _extract_csv_value(field, row, header)
                source_row[column] = value
                expected_value = _locally_transformable_value(field, value, macros, warnings)
                if expected_value is not _SKIP_EXPECTED:
                    expected_row[field["id"]] = expected_value
            source_rows.append(source_row)
            expected_rows.append(expected_row)
    return source_rows, expected_rows, warnings


_SKIP_EXPECTED = object()


def _extract_csv_value(field: dict[str, Any], row: list[str], header: list[str] | None) -> str | None:
    source = field.get("source", {})
    pos = source.get("pos")
    if isinstance(pos, int):
        return row[pos] if pos < len(row) else None
    column = source.get("column")
    if isinstance(column, str) and header is not None and column in header:
        index = header.index(column)
        return row[index] if index < len(row) else None
    return None


def _locally_transformable_value(
    field: dict[str, Any],
    value: Any,
    macros: PythonMacroResolver,
    warnings: list[Diagnostic],
) -> Any:
    current = value
    for transform in field.get("transforms", []):
        transform_type = transform.get("type")
        if transform_type == "trim":
            current = None if current is None else str(current).strip()
        elif transform_type == "round":
            continue
        elif transform_type == "custom":
            macro_name = transform["macro"]
            if not macros.has_python_callable(macro_name):
                warnings.append(
                    Diagnostic(
                        "field omitted from unit-test expectation because custom transform has no Python execution callable",
                        macro_name,
                    )
                )
                return _SKIP_EXPECTED
            current = macros.call(macro_name, value=current, field=field, rule=transform)
        else:
            warnings.append(
                Diagnostic(
                    f"field omitted from unit-test expectation because transform `{transform_type}` is not implemented locally",
                    field["id"],
                )
            )
            return _SKIP_EXPECTED
    return current


def _field_expression(field: dict[str, Any]) -> str:
    source = field.get("source", {})
    expression = _quote_identifier(source.get("column", field["id"]))
    for transform in field.get("transforms", []):
        transform_type = transform.get("type")
        if transform_type == "trim":
            side = transform.get("side", "both")
            if side == "left":
                expression = f"ltrim({expression})"
            elif side == "right":
                expression = f"rtrim({expression})"
            else:
                expression = f"trim({expression})"
        elif transform_type == "round":
            expression = f"round({expression}, {int(transform['scale'])})"
        elif transform_type == "custom":
            macro_name = _macro_object_name(transform["macro"])
            expression = "{{ " + f"{macro_name}('{expression}')" + " }}"
        elif transform_type in {"parse_date", "parse_timestamp"}:
            raise ValueError(f"`{transform_type}` is not implemented in dbt generation yet")
    return expression


def _custom_macro_names(spec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for field in fields(spec):
        for rule_group_name in ("transforms", "validations"):
            for rule in field.get(rule_group_name, []):
                if isinstance(rule, dict) and rule.get("type") == "custom":
                    names.append(rule["macro"])
    return sorted(set(names))


def _source_model_name(target_id: str) -> str:
    return f"{target_id}__csv_stage"


def _csv_stage_location(source: dict[str, Any]) -> str:
    location = source.get("location", {})
    if not isinstance(location, dict):
        location = {}
    database = location.get("database")
    schema = location.get("schema", "AD_HOC")
    stage = location.get("stage", "@csv_stage")
    filename = location.get("filename")
    parts = []
    if database:
        parts.append(str(database))
    if schema:
        parts.append(str(schema))
    stage_text = str(stage)
    if stage_text.startswith("@"):
        stage_text = stage_text[1:]
    parts.append(stage_text)
    relation = ".".join(parts)
    if filename:
        relation = f"{relation}/{filename}"
    return _stage_reference(relation)


def _stage_reference(value: str) -> str:
    return value if value.startswith("@") else f"@{value}"


def _macro_object_name(macro_name: str) -> str:
    return macro_name.rpartition(".")[2]


def _quote_identifier(value: str) -> str:
    return value


def _write(path: Path, content: str, result: DbtGenerationResult) -> None:
    path.write_text(content, encoding="utf-8")
    result.files.append(path)


def _dedupe_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    seen: set[tuple[str | None, str]] = set()
    deduped: list[Diagnostic] = []
    for diagnostic in diagnostics:
        key = (diagnostic.location, diagnostic.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(diagnostic)
    return deduped
