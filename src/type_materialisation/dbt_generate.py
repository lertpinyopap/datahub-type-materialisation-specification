import csv
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .custom_macros import MacroLoadError, PythonMacroResolver
from .errors import Diagnostic
from .inheritance import InheritanceError, resolve_spec
from .schema import require_yaml
from .spec import GENERATED_METADATA_FIELD_TYPES, fields, parse_sql_type


DBT_PROJECT_NAME = "type_materialisation_generated"
DBT_PROFILE_NAME = "datahub_type_materialisation"

NOT_IMPLEMENTED = [
    "SCD1 and SCD2 materialisation",
    "generated Snowflake file-format objects for CSV stages",
    "Python upload of local CSV files to Snowflake stages",
    "date/timestamp format translation from Python strptime to Snowflake formats",
    "multi-error quarantine output",
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
    spec: dict[str, Any] | None = None
    spec_paths: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class RelationConfig:
    database: str | None
    schema: str
    table: str


def generate_dbt_project(options: GenerateDbtOptions) -> DbtGenerationResult:
    result = DbtGenerationResult(output_dir=options.output_dir)
    if options.output_dir.exists() and any(options.output_dir.iterdir()):
        result.errors.append(
            Diagnostic(
                "output directory already exists and is not empty; choose an empty directory for dbt generation",
                str(options.output_dir),
            )
        )
        return result
    if options.spec is None:
        try:
            spec = resolve_spec(options.spec_path, spec_paths=options.spec_paths).spec
        except InheritanceError as exc:
            result.errors.append(Diagnostic(str(exc), "inheritance"))
            return result
    else:
        spec = options.spec
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
    _write_project_file(options.output_dir, spec, options.spec_path.name, result)
    _write_status_file(options.output_dir, result)
    _write_source_model(spec, options, result)
    if result.errors:
        return result
    try:
        _write_final_model(spec, result)
    except ValueError as exc:
        result.errors.append(Diagnostic(str(exc), "$.target.fields"))
        return result
    if _failure_mode(spec) == "fail_file":
        _write_validation_guard_model(spec, result)
    _write_generated_macros(options.output_dir, generated_macros, result)
    if options.unit_test_csv is not None:
        _write_unit_tests(spec, options, macros, result)
    result.warnings = _dedupe_diagnostics(result.warnings)
    return result


def _unsupported_for_initial_dbt_generation(spec: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    source = spec.get("source", {})
    control_data = spec.get("control_data", {})
    if not isinstance(source, dict) or source.get("format") not in {"csv", "table"}:
        diagnostics.append(Diagnostic("dbt generation supports source.format = csv or table", "$.source.format"))
    if isinstance(control_data, dict) and control_data.get("change_type") in {"scd1", "scd2"}:
        diagnostics.append(Diagnostic("SCD materialisation is not implemented yet", "$.control_data.change_type"))
    return diagnostics


def _ensure_dirs(output_dir: Path) -> None:
    (output_dir / "models" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "reference").mkdir(parents=True, exist_ok=True)
    (output_dir / "seeds").mkdir(parents=True, exist_ok=True)


def _write_project_file(output_dir: Path, spec: dict[str, Any], spec_file_name: str, result: DbtGenerationResult) -> None:
    yaml = require_yaml()
    pre_hooks, post_hooks = _job_hooks(spec, spec_file_name)
    project = {
        "name": DBT_PROJECT_NAME,
        "version": "1.0",
        "config-version": 2,
        "profile": DBT_PROFILE_NAME,
        "model-paths": ["models"],
        "seed-paths": ["seeds"],
        "macro-paths": ["macros"],
        "on-run-start": pre_hooks,
        "on-run-end": post_hooks,
        "models": {
            DBT_PROJECT_NAME: {
                "generated": {
                    "+materialized": "view",
                }
            }
        },
    }
    seed_config = _seed_project_config(spec)
    if seed_config is not None:
        project["seeds"] = seed_config
    content = yaml.safe_dump(project, sort_keys=False, width=10_000)
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
    source = spec["source"]
    if source["format"] == "table":
        _write_table_source_model(spec, options, result)
    else:
        _write_csv_source_model(spec, options, result)


def _write_csv_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    if _csv_load_method(spec["source"]) == "dbt_seed":
        _write_csv_seed_source_model(spec, options, result)
        return

    target = spec["target"]
    model_name = _source_model_name(target["id"])
    stage = _stage_reference(options.csv_stage) if options.csv_stage else _csv_stage_location(spec["source"])
    select_lines = []
    for field in fields(spec):
        source = field.get("source", {})
        pos = source.get("pos")
        column = _source_column_name(field)
        ordinal = int(pos) + 1
        select_lines.append(f"    ${ordinal}::string as {_quote_identifier(column)}")
    sql = "\n".join(
        [
            f"{{{{ config(materialized='view', alias='{_physical_name(model_name)}') }}}}",
            "",
            "select",
            ",\n".join(select_lines),
            f"from {stage}",
            "",
        ]
    )
    _write(options.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _write_csv_seed_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    target = spec["target"]
    model_name = _source_model_name(target["id"])
    seed_name = _csv_seed_name(spec)
    try:
        seed_path = _write_csv_seed_file(spec, options, result)
    except OSError as exc:
        result.errors.append(Diagnostic(str(exc), "$.source.seed.file"))
        return

    select_lines = []
    for field in fields(spec):
        column = _source_column_name(field)
        select_lines.append(f"    cast({_quote_identifier(column)} as string) as {_quote_identifier(column)}")
    sql = "\n".join(
        [
            f"{{{{ config(materialized='view', alias='{_physical_name(model_name)}') }}}}",
            "",
            "select",
            ",\n".join(select_lines),
            f"from {{{{ ref('{seed_name}') }}}}",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)
    result.files.append(seed_path)


def _write_csv_seed_file(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> Path:
    seed_file = _csv_seed_file_path(spec, options.spec_path)
    seed_name = _csv_seed_name(spec)
    target_path = result.output_dir / "seeds" / f"{seed_name}.csv"
    if spec["source"].get("header") is False:
        _write_headerless_seed_file(spec, seed_file, target_path)
    else:
        shutil.copyfile(seed_file, target_path)
    return target_path


def _write_headerless_seed_file(spec: dict[str, Any], seed_file: Path, target_path: Path) -> None:
    source = spec["source"]
    dialect = {
        "delimiter": source.get("delimiter", ","),
        "quotechar": source.get("quotechar", '"'),
        "lineterminator": source.get("lineterminator", "\r\n"),
    }
    with seed_file.open("r", encoding="utf-8", newline="") as source_handle:
        reader = csv.reader(
            source_handle,
            delimiter=dialect["delimiter"],
            quotechar=dialect["quotechar"],
        )
        rows = list(reader)
        max_pos = max(
            int(field.get("source", {}).get("pos", 0))
            for field in fields(spec)
        )
        column_count = max([max_pos + 1, *(len(row) for row in rows)])
        header = [_position_column_name(index) for index in range(column_count)]
        with target_path.open("w", encoding="utf-8", newline="") as target_handle:
            writer = csv.writer(
                target_handle,
                delimiter=dialect["delimiter"],
                quotechar=dialect["quotechar"],
                lineterminator=dialect["lineterminator"],
            )
            writer.writerow(header)
            writer.writerows(rows)


def _write_table_source_model(spec: dict[str, Any], options: GenerateDbtOptions, result: DbtGenerationResult) -> None:
    del options
    source = spec["source"]
    target = spec["target"]
    model_name = _source_model_name(target["id"])
    select_lines = []
    for field in fields(spec):
        column = field.get("source", {}).get("column", field["id"])
        select_lines.append(f"    {_quote_identifier(column)} as {_quote_identifier(column)}")
    sql = "\n".join(
        [
            f"{{{{ config(materialized='view', alias='{_physical_name(model_name)}') }}}}",
            "",
            "select",
            ",\n".join(select_lines),
            f"from {_table_source_relation(source)}",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _write_final_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    table_name = target.get("table_name", target["id"])
    materialized = spec.get("control_data", {}).get("materialisation_type", "view")
    source_model = _source_model_name(target["id"])
    quarantine_enabled = _quarantine_enabled(spec)
    fail_file_enabled = _failure_mode(spec) == "fail_file"
    select_lines = []
    for field in fields(spec):
        expression = _field_expression(field)
        data_type = field["data_type"]
        select_lines.append(f"    cast({expression} as {data_type}) as {_quote_identifier(field['id'])}")
    select_lines.extend(
        [
            "    cast('{{ var(\"audit_data_process_key\", \"manual\") }}' "
            f"as {GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}) as AUDIT_DATA_PROCESS_KEY",
            f"    cast(current_timestamp() as {GENERATED_METADATA_FIELD_TYPES['audit_created_datetime']}) "
            "as AUDIT_CREATED_DATETIME",
            f"    cast(current_timestamp() as {GENERATED_METADATA_FIELD_TYPES['audit_last_changed_datetime']}) "
            "as AUDIT_LAST_CHANGED_DATETIME",
        ]
    )
    config_lines = _model_config_lines(
        materialized=materialized,
        schema=target["schema"],
        alias=table_name,
        database=target.get("database"),
    )
    if quarantine_enabled:
        body_lines = [
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=True),
            *_valid_rows_cte(),
            "",
            "select",
            ",\n".join(select_lines),
            "from valid_rows",
            "",
        ]
    elif fail_file_enabled:
        validation_guard_model = _validation_guard_model_name(target["id"])
        body_lines = [
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=True),
            "valid_rows as (",
            "    select validation_rows.*",
            "    from validation_rows",
            f"    cross join {{{{ ref('{validation_guard_model}') }}}} as validation_guard",
            "    where validation_rows.FAILURE_DETAILS is null",
            "      and validation_guard.VALIDATION_FAILURE_GUARD = 0",
            ")",
            "",
            "select",
            ",\n".join(select_lines),
            "from valid_rows",
            "",
        ]
    else:
        body_lines = [
            "select",
            ",\n".join(select_lines),
            f"from {{{{ ref('{source_model}') }}}}",
            "",
        ]
    sql = "\n".join(["{{", "  config(", *config_lines, "  )", "}}", "", *body_lines])
    _write(result.output_dir / "models" / "generated" / f"{target['id']}.sql", sql, result)
    if quarantine_enabled:
        _write_quarantine_model(spec, result)


def _write_validation_guard_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    relation = _target_relation_config(spec)
    source_model = _source_model_name(target["id"])
    model_name = _validation_guard_model_name(target["id"])
    alias = f"{relation.table}__VALIDATION_GUARD"
    config_lines = _model_config_lines(
        materialized="table",
        database=relation.database,
        schema=relation.schema,
        alias=alias,
    )
    sql = "\n".join(
        [
            "{{",
            "  config(",
            *config_lines,
            "  )",
            "}}",
            "",
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=False),
            "",
            "select",
            "    case",
            "        when count(*) = 0 then 0",
            "        else cast('TYPE_MATERIALISATION_VALIDATION_FAILED' as number)",
            "    end as VALIDATION_FAILURE_GUARD",
            "from validation_rows",
            "where FAILURE_DETAILS is not null",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _model_config_lines(
    *,
    materialized: str,
    schema: str,
    alias: str,
    database: str | None,
    extra_config_lines: list[str] | None = None,
) -> list[str]:
    config_lines = [
        f"    materialized='{materialized}',",
        f"    schema='{_physical_name(schema)}',",
        f"    alias='{_physical_name(alias)}',",
    ]
    if database:
        config_lines.insert(1, f"    database='{_physical_name(database)}',")
    if extra_config_lines:
        config_lines.extend(extra_config_lines)
    return config_lines


def _write_quarantine_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    quarantine = _quarantine_relation_config(spec)
    model_name = _quarantine_model_name(target["id"])
    source_model = _source_model_name(target["id"])
    source_columns = _source_output_columns(spec)
    select_lines = [
        "    cast(current_timestamp() as timestamp_tz) as LOADED_AT",
        f"    {_job_id_expression()} as JOB_ID",
        "    FAILURE_DETAILS",
    ]
    select_lines.extend(f"    {_quote_identifier(column)}" for column in source_columns)
    config_lines = _model_config_lines(
        materialized="incremental",
        database=quarantine.database,
        schema=quarantine.schema,
        alias=quarantine.table,
        extra_config_lines=[
            "    incremental_strategy='append',",
            "    on_schema_change='append_new_columns',",
        ],
    )
    sql = "\n".join(
        [
            "{{",
            "  config(",
            *config_lines,
            "  )",
            "}}",
            "",
            "with source_rows as (",
            f"    select * from {{{{ ref('{source_model}') }}}}",
            "),",
            *_validation_rows_cte(spec, trailing_comma=False),
            "",
            "select",
            ",\n".join(select_lines),
            "from validation_rows",
            "where FAILURE_DETAILS is not null",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


def _write_generated_macros(output_dir: Path, macros: dict[str, str], result: DbtGenerationResult) -> None:
    _write(output_dir / "macros" / "generated" / "generate_schema_name.sql", _generate_schema_name_macro(), result)
    for macro_name, macro_sql in sorted(macros.items()):
        _write(output_dir / "macros" / "generated" / f"{macro_name}.sql", macro_sql + "\n", result)


def _generate_schema_name_macro() -> str:
    return "\n".join(
        [
            "{% macro generate_schema_name(custom_schema_name, node) -%}",
            "    {%- set override_schema = var('target_schema', none) -%}",
            "    {%- if override_schema is not none -%}",
            "        {{ override_schema | trim | upper }}",
            "    {%- elif custom_schema_name is none -%}",
            "        {{ target.schema | upper }}",
            "    {%- else -%}",
            "        {{ custom_schema_name | trim | upper }}",
            "    {%- endif -%}",
            "{%- endmacro %}",
            "",
        ]
    )


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
    source_fixture_sql = _unit_test_fixture_sql(source_rows, _source_fixture_column_types(spec))
    unit_tests = [
        {
            "name": f"{target['id']}_sample_source",
            "model": target["id"],
            "given": [
                {
                    "input": f"ref('{_source_model_name(target['id'])}')",
                    "format": "sql",
                    "rows": source_fixture_sql,
                }
            ],
            "expect": {
                "rows": expected_rows,
            },
        }
    ]
    if _quarantine_enabled(spec):
        unit_tests.append(
            {
                "name": f"{_quarantine_model_name(target['id'])}_sample_source",
                "model": _quarantine_model_name(target["id"]),
                "given": [
                    {
                        "input": f"ref('{_source_model_name(target['id'])}')",
                        "format": "sql",
                        "rows": source_fixture_sql,
                    }
                ],
                "expect": {
                    "rows": [],
                },
            }
        )
    data = {"unit_tests": unit_tests}
    content = yaml.safe_dump(data, sort_keys=False)
    _write(result.output_dir / "models" / "generated" / f"{target['id']}_unit_tests.yml", content, result)


def _unit_test_fixture_sql(rows: list[dict[str, Any]], column_types: dict[str, str]) -> str:
    if not rows:
        select_lines = [
            f"    cast(null as {data_type}) as {_quote_identifier(column)}"
            for column, data_type in column_types.items()
        ]
        return "\n".join(["select", ",\n".join(select_lines), "where 1 = 0"])

    selects = []
    for row in rows:
        select_lines = []
        for column, data_type in column_types.items():
            value = row.get(column)
            select_lines.append(f"    {_sql_literal(value, data_type)} as {_quote_identifier(column)}")
        selects.append("\n".join(["select", ",\n".join(select_lines)]))
    return "\nunion all\n".join(selects)


def _source_fixture_column_types(spec: dict[str, Any]) -> dict[str, str]:
    return {
        column: "varchar"
        for column in _source_output_columns(spec)
    }


def _unit_test_rows(
    spec: dict[str, Any],
    csv_path: Path,
    macros: PythonMacroResolver,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Diagnostic]]:
    warnings: list[Diagnostic] = []
    source = spec["source"]
    target_fields = fields(spec)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=source.get("delimiter", ","), quotechar=source.get("quotechar", '"'))
        header = next(reader) if source.get("header") else None
        source_rows: list[dict[str, Any]] = []
        expected_rows: list[dict[str, Any]] = []
        for row in reader:
            source_row: dict[str, Any] = {}
            expected_row: dict[str, Any] = {}
            for field in target_fields:
                column = _source_column_name(field)
                value = _extract_csv_value(field, row, header)
                source_row[column] = value
                expected_value = _locally_transformable_value(field, value, macros, warnings)
                if expected_value is not _SKIP_EXPECTED:
                    expected_row[_physical_name(field["id"])] = expected_value
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
    expression = _quote_identifier(_source_column_name(field))
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


def _failure_details_expression(spec: dict[str, Any]) -> str:
    expressions = []
    for field in fields(spec):
        expressions.extend(_field_failure_expressions(field))
        expressions.extend(_field_uniqueness_failure_expressions(field))
    if not expressions:
        return "null"
    return f"coalesce({', '.join(expressions)})"


def _field_failure_expressions(field: dict[str, Any]) -> list[str]:
    field_id = field["id"]
    expression = _field_expression(field)
    data_type = field["data_type"]
    failures: list[str] = []
    if field.get("nullable") is False:
        failures.append(
            f"case when {expression} is null then {_sql_string(f'field `{field_id}` is null but not nullable')} end"
        )

    sql_type = parse_sql_type(data_type)
    if sql_type.name in {"char", "varchar", "character", "string"} and sql_type.args:
        max_length = sql_type.args[0]
        failures.append(
            "case "
            f"when {expression} is not null and length({expression}) > {max_length} "
            f"then {_sql_string(f'field `{field_id}` exceeds data_type `{data_type}`')} "
            "end"
        )
    elif sql_type.name not in {"text"}:
        failures.append(
            "case "
            f"when {expression} is not null and try_cast({expression} as {data_type}) is null "
            f"then {_sql_string(f'field `{field_id}` does not match data_type `{data_type}`')} "
            "end"
        )

    for rule in field.get("validations", []):
        if not isinstance(rule, dict):
            continue
        rule_type = rule.get("type")
        if rule_type == "min_length":
            failures.append(
                "case "
                f"when {expression} is not null and length({expression}) < {int(rule['value'])} "
                f"then {_sql_string(f'field `{field_id}` length is less than {rule['value']}')} "
                "end"
            )
        elif rule_type == "max_length":
            failures.append(
                "case "
                f"when {expression} is not null and length({expression}) > {int(rule['value'])} "
                f"then {_sql_string(f'field `{field_id}` length is greater than {rule['value']}')} "
                "end"
            )
        elif rule_type == "regex":
            failures.append(
                "case "
                f"when {expression} is not null and not regexp_like({expression}, {_sql_string(str(rule['pattern']))}) "
                f"then {_sql_string(f'field `{field_id}` does not match regex')} "
                "end"
            )
        elif rule_type == "allowed_values":
            values = ", ".join(_sql_string(str(value)) for value in rule.get("values", []))
            failures.append(
                "case "
                f"when {expression} is not null and {expression} not in ({values}) "
                f"then {_sql_string(f'field `{field_id}` is not one of the allowed values')} "
                "end"
            )
        elif rule_type == "min_value":
            failures.append(
                "case "
                f"when {expression} is not null and try_cast({expression} as number) < {rule['value']} "
                f"then {_sql_string(f'field `{field_id}` is less than {rule['value']}')} "
                "end"
            )
        elif rule_type == "max_value":
            failures.append(
                "case "
                f"when {expression} is not null and try_cast({expression} as number) > {rule['value']} "
                f"then {_sql_string(f'field `{field_id}` is greater than {rule['value']}')} "
                "end"
            )
        elif rule_type == "precision":
            precision = int(rule["precision"])
            scale = int(rule["scale"]) if "scale" in rule else 0
            failures.append(
                "case "
                f"when {expression} is not null and try_cast({expression} as number({precision}, {scale})) is null "
                f"then {_sql_string(f'field `{field_id}` does not fit validation precision/scale')} "
                "end"
            )
        elif rule_type == "custom":
            macro_name = _macro_object_name(rule["macro"])
            failures.append("{{ " + f"{macro_name}('{expression}')" + " }}")
    return failures


def _field_uniqueness_failure_expressions(field: dict[str, Any]) -> list[str]:
    if field.get("unique") is not True:
        return []
    field_id = field["id"]
    expression = _field_expression(field)
    typed_expression = f"try_cast({expression} as {field['data_type']})"
    return [
        "case "
        f"when {typed_expression} is not null "
        f"and count(*) over (partition by {typed_expression}) > 1 "
        f"then {_sql_string(f'field `{field_id}` duplicates a value for a unique field')} "
        "end"
    ]


def _validation_rows_cte(spec: dict[str, Any], *, trailing_comma: bool) -> list[str]:
    suffix = "," if trailing_comma else ""
    return [
        "validation_rows as (",
        "    select",
        "        *,",
        f"        {_failure_details_expression(spec)} as FAILURE_DETAILS",
        "    from source_rows",
        f"){suffix}",
    ]


def _valid_rows_cte() -> list[str]:
    return [
        "valid_rows as (",
        "    select *",
        "    from validation_rows",
        "    where FAILURE_DETAILS is null",
        ")",
    ]


def _custom_macro_names(spec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for field in fields(spec):
        for rule_group_name in ("transforms", "validations"):
            for rule in field.get(rule_group_name, []):
                if isinstance(rule, dict) and rule.get("type") == "custom":
                    names.append(rule["macro"])
    return sorted(set(names))


def _source_model_name(target_id: str) -> str:
    return f"{target_id}__source"


def _quarantine_model_name(target_id: str) -> str:
    return f"{target_id}__quarantine"


def _validation_guard_model_name(target_id: str) -> str:
    return f"{target_id}__validation_guard"


def _csv_load_method(source: dict[str, Any]) -> str:
    return str(source.get("load_method", "stage"))


def _csv_seed_name(spec: dict[str, Any]) -> str:
    seed = spec["source"].get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    return str(seed.get("name", f"{spec['target']['id']}__seed"))


def _csv_seed_file_path(spec: dict[str, Any], spec_path: Path) -> Path:
    seed = spec["source"].get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    raw_path = seed.get("file")
    if not isinstance(raw_path, str):
        raise OSError("source.seed.file is required when source.load_method is dbt_seed")
    path = Path(raw_path)
    if not path.is_absolute():
        path = spec_path.parent / path
    if not path.exists():
        raise OSError(f"seed CSV file does not exist: {path}")
    return path


def _seed_project_config(spec: dict[str, Any]) -> dict[str, Any] | None:
    source = spec.get("source", {})
    if not isinstance(source, dict) or source.get("format") != "csv" or _csv_load_method(source) != "dbt_seed":
        return None

    target = spec["target"]
    seed = source.get("seed", {})
    if not isinstance(seed, dict):
        seed = {}
    seed_name = _csv_seed_name(spec)
    config: dict[str, Any] = {
        "+quote_columns": False,
        "+schema": _physical_name(seed.get("schema", target["schema"])),
        "+alias": _physical_name(seed_name),
        "+column_types": {
            _physical_name(column): "varchar"
            for column in _source_output_columns(spec)
        },
    }
    if seed.get("database"):
        config["+database"] = _physical_name(seed["database"])
    if source.get("delimiter", ",") != ",":
        config["+delimiter"] = source["delimiter"]
    return {DBT_PROJECT_NAME: {seed_name: config}}


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
        parts.append(_physical_name(database))
    if schema:
        parts.append(_physical_name(schema))
    stage_text = str(stage)
    if stage_text.startswith("@"):
        stage_text = stage_text[1:]
    parts.append(_physical_name(stage_text))
    relation = ".".join(parts)
    if filename:
        relation = f"{relation}/{filename}"
    return _stage_reference(relation)


def _table_source_relation(source: dict[str, Any]) -> str:
    parts = []
    if source.get("database"):
        parts.append(_physical_name(source["database"]))
    parts.extend([_physical_name(source["schema"]), _physical_name(source["table"])])
    return ".".join(parts)


def _stage_reference(value: str) -> str:
    text = value[1:] if value.startswith("@") else value
    relation, separator, path = text.partition("/")
    stage_reference = _physical_name(relation)
    if separator:
        stage_reference = f"{stage_reference}/{path}"
    return f"@{stage_reference}"


def _macro_object_name(macro_name: str) -> str:
    return macro_name.rpartition(".")[2]


def _quote_identifier(value: str) -> str:
    return _physical_name(value)


def _physical_name(value: Any) -> str:
    text = str(value)
    if "{{" in text:
        return text
    return text.upper()


def _quarantine_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    return isinstance(control_data, dict) and control_data.get("failure_mode") == "quarantine_row"


def _failure_mode(spec: dict[str, Any]) -> str:
    control_data = spec.get("control_data", {})
    if not isinstance(control_data, dict):
        return "fail_file"
    return str(control_data.get("failure_mode", "fail_file"))


def _target_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = spec["target"]
    return RelationConfig(
        database=_physical_name(target["database"]) if target.get("database") else None,
        schema=_physical_name(target["schema"]),
        table=_physical_name(target.get("table_name", target["id"])),
    )


def _quarantine_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = _target_relation_config(spec)
    quarantine = spec.get("control_data", {}).get("quarantine", {})
    if not isinstance(quarantine, dict):
        quarantine = {}
    return RelationConfig(
        database=_physical_name(quarantine["database"]) if quarantine.get("database") else target.database,
        schema=_physical_name(quarantine["schema"]) if quarantine.get("schema") else target.schema,
        table=_physical_name(quarantine.get("table", f"{target.table}_QUARANTINE")),
    )


def _job_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = _target_relation_config(spec)
    job = spec.get("control_data", {}).get("job", {})
    if not isinstance(job, dict):
        job = {}
    job_schema = _physical_name(job.get("schema", "BUSINESS"))
    return RelationConfig(
        database=_physical_name(job["database"]) if job.get("database") else target.database,
        schema=f"{{{{ var('tms_job_schema', '{job_schema}') | upper }}}}",
        table=_physical_name(job.get("table", "TYPE_MATERIALISATION_JOBS")),
    )


def _relation_name(relation: RelationConfig) -> str:
    parts = []
    if relation.database:
        parts.append(relation.database)
    parts.extend([relation.schema, relation.table])
    return ".".join(parts)


def _runtime_relation_label(relation: RelationConfig, *, schema_var: str | None = None) -> str:
    database = relation.database or "{{ target.database | upper }}"
    if schema_var is None:
        schema = relation.schema
    else:
        schema = '{{ var("' + schema_var + '", "' + relation.schema + '") | upper }}'
    return ".".join([database, schema, relation.table])


def _dbt_relation_lookup(relation: RelationConfig, variable_name: str, *, schema_var: str | None = None) -> str:
    database = _sql_string(relation.database) if relation.database else "(target.database | upper)"
    if schema_var is None:
        schema = _sql_string(relation.schema)
    else:
        schema = f'(var("{schema_var}", "{relation.schema}") | upper)'
    return (
        "{% set "
        + variable_name
        + " = adapter.get_relation(database="
        + database
        + ", schema="
        + schema
        + ", identifier="
        + _sql_string(_physical_name(relation.table))
        + ") %}"
    )


def _count_expression(relation_variable_name: str) -> str:
    return (
        "{% if "
        + relation_variable_name
        + " is not none %}(select count(*) from {{ "
        + relation_variable_name
        + " }}){% else %}null{% endif %}"
    )


def _count_cte(relation_variable_name: str, cte_name: str, column_name: str) -> str:
    return (
        cte_name
        + " as (select "
        + _count_expression(relation_variable_name)
        + " as "
        + _physical_name(column_name)
        + ")"
    )


def _job_id_expression() -> str:
    return "cast('{{ invocation_id }}' as varchar(64))"


def _source_output_columns(spec: dict[str, Any]) -> list[str]:
    target_fields = fields(spec)
    if spec["source"]["format"] == "csv":
        target_fields = sorted(
            target_fields,
            key=lambda field: field.get("source", {}).get("pos", 999999),
        )
    return [_source_column_name(field) for field in target_fields]


def _source_column_name(field: dict[str, Any]) -> str:
    source = field.get("source", {})
    column = source.get("column")
    if isinstance(column, str):
        return _physical_name(column)
    pos = source.get("pos")
    if isinstance(pos, int):
        return _position_column_name(pos)
    return _physical_name(field["id"])


def _position_column_name(pos: int) -> str:
    return f"COL_{pos}"


def _job_hooks(spec: dict[str, Any], spec_file_name: str) -> tuple[list[str], list[str]]:
    relation = _relation_name(_job_relation_config(spec))
    target_relation = _target_relation_config(spec)
    generated_table = _runtime_relation_label(target_relation, schema_var="target_schema")
    quarantine_table = (
        _runtime_relation_label(_quarantine_relation_config(spec), schema_var="target_schema")
        if _quarantine_enabled(spec)
        else None
    )
    generated_relation_lookup = _dbt_relation_lookup(
        target_relation,
        "tms_generated_relation",
        schema_var="target_schema",
    )
    quarantine_relation_lookup = (
        _dbt_relation_lookup(
            _quarantine_relation_config(spec),
            "tms_quarantine_relation",
            schema_var="target_schema",
        )
        if _quarantine_enabled(spec)
        else "{% set tms_quarantine_relation = none %}"
    )
    result_expression = (
        "{% set failed_result_count = "
        "(results | selectattr('status', 'equalto', 'error') | list | length) + "
        "(results | selectattr('status', 'equalto', 'fail') | list | length) %}"
        "{% if failed_result_count > 0 %}'FAILED'{% else %}"
        "case when quarantine_counts.QUARANTINE_COUNT > 0 "
        "then 'COMPLETED_WITH_QUARANTINE' else 'COMPLETED' end{% endif %}"
    )
    details_expression = (
        "{% set explicit_job_details = var(\"job_details\", none) %}"
        "{% if explicit_job_details is not none %}"
        "'{{ explicit_job_details | replace(\"'\", \"''\") }}'"
        "{% elif failed_result_count > 0 %}"
        "'dbt run failed; inspect dbt artifacts and quarantine output for validation details'"
        "{% else %}case when quarantine_counts.QUARANTINE_COUNT > 0 "
        "then 'validation errors written to quarantine output' else null end{% endif %}"
    )
    create_sql = (
        f"create table if not exists {relation} ("
        "JOB_ID varchar(64), "
        "EVENT_TYPE varchar(32), "
        "EVENT_TIMESTAMP timestamp_tz, "
        "RESULT varchar(64), "
        "DETAILS varchar(16777216), "
        "SPEC_FILE_NAME varchar(1024), "
        "GENERATED_TABLE varchar(1024), "
        "QUARANTINE_TABLE varchar(1024), "
        "LOADED_COUNT number(38, 0), "
        "QUARANTINE_COUNT number(38, 0), "
        f"AUDIT_DATA_PROCESS_KEY {GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}, "
        f"AUDIT_CREATED_DATETIME {GENERATED_METADATA_FIELD_TYPES['audit_created_datetime']}, "
        f"AUDIT_LAST_CHANGED_DATETIME {GENERATED_METADATA_FIELD_TYPES['audit_last_changed_datetime']}"
        ")"
    )
    start_sql = (
        f"insert into {relation} "
        "(JOB_ID, EVENT_TYPE, EVENT_TIMESTAMP, RESULT, DETAILS, SPEC_FILE_NAME, GENERATED_TABLE, "
        "QUARANTINE_TABLE, LOADED_COUNT, QUARANTINE_COUNT, AUDIT_DATA_PROCESS_KEY, "
        "AUDIT_CREATED_DATETIME, AUDIT_LAST_CHANGED_DATETIME) select "
        f"{_job_id_expression()}, "
        "'JOB_START', "
        "cast(current_timestamp() as timestamp_tz), "
        "null, "
        "null, "
        f"cast({_sql_string(spec_file_name)} as varchar(1024)), "
        f"cast({_sql_string(generated_table)} as varchar(1024)), "
        f"{_nullable_sql_string(quarantine_table)}, "
        "cast(null as number(38, 0)), "
        "cast(null as number(38, 0)), "
        f"cast('{{{{ var(\"audit_data_process_key\", \"manual\") }}}}' as "
        f"{GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}), "
        "cast(current_timestamp() as timestamp_tz), "
        "cast(current_timestamp() as timestamp_tz)"
    )
    end_sql = (
        f"{generated_relation_lookup}{quarantine_relation_lookup}insert into {relation} "
        "(JOB_ID, EVENT_TYPE, EVENT_TIMESTAMP, RESULT, DETAILS, SPEC_FILE_NAME, GENERATED_TABLE, "
        "QUARANTINE_TABLE, LOADED_COUNT, QUARANTINE_COUNT, AUDIT_DATA_PROCESS_KEY, "
        "AUDIT_CREATED_DATETIME, AUDIT_LAST_CHANGED_DATETIME) "
        "with "
        f"{_count_cte('tms_generated_relation', 'loaded_counts', 'loaded_count')}, "
        f"{_count_cte('tms_quarantine_relation', 'quarantine_counts', 'quarantine_count')} "
        "select "
        f"{_job_id_expression()}, "
        "'JOB_END', "
        "cast(current_timestamp() as timestamp_tz), "
        f"{result_expression}, "
        f"{details_expression}, "
        f"cast({_sql_string(spec_file_name)} as varchar(1024)), "
        f"cast({_sql_string(generated_table)} as varchar(1024)), "
        f"{_nullable_sql_string(quarantine_table)}, "
        "loaded_counts.LOADED_COUNT, "
        "quarantine_counts.QUARANTINE_COUNT, "
        f"cast('{{{{ var(\"audit_data_process_key\", \"manual\") }}}}' as "
        f"{GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}), "
        "cast(current_timestamp() as timestamp_tz), "
        "cast(current_timestamp() as timestamp_tz) "
        "from loaded_counts cross join quarantine_counts"
    )
    return (
        [_optional_job_hook(create_sql), _optional_job_hook(start_sql)],
        [_optional_job_hook(create_sql), _optional_job_hook(end_sql)],
    )


def _optional_job_hook(sql: str) -> str:
    return "{% if var('tms_enable_job_hooks', true) %}" + sql + "{% endif %}"


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _nullable_sql_string(value: str | None) -> str:
    if value is None:
        return "cast(null as varchar(1024))"
    return f"cast({_sql_string(value)} as varchar(1024))"


def _sql_literal(value: Any, data_type: str) -> str:
    if value is None:
        return f"cast(null as {data_type})"
    return f"cast({_sql_string(str(value))} as {data_type})"


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
