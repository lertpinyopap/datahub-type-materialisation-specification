import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .custom_macros import MacroLoadError, PythonMacroResolver
from .errors import Diagnostic
from .inheritance import InheritanceError, resolve_spec
from .schema import require_yaml
from .spec import GENERATED_METADATA_FIELD_TYPES, fields, parse_sql_type


NOT_IMPLEMENTED = [
    "SCD1 and SCD2 materialisation",
    "uniqueness checks in generated dbt SQL",
    "fail_file validation failure enforcement in generated dbt SQL",
    "generated Snowflake file-format objects for CSV stages",
    "Python upload of local CSV files to Snowflake stages",
    "date/timestamp format translation from Python strptime to Snowflake formats",
    "custom Python execution inside dbt macros",
    "database-native Python UDF generation",
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
    _write_project_file(options.output_dir, spec, result)
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
    if not isinstance(source, dict) or source.get("format") not in {"csv", "table"}:
        diagnostics.append(Diagnostic("dbt generation supports source.format = csv or table", "$.source.format"))
    if isinstance(control_data, dict) and control_data.get("change_type") in {"scd1", "scd2"}:
        diagnostics.append(Diagnostic("SCD materialisation is not implemented yet", "$.control_data.change_type"))
    return diagnostics


def _ensure_dirs(output_dir: Path) -> None:
    (output_dir / "models" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "generated").mkdir(parents=True, exist_ok=True)
    (output_dir / "macros" / "reference").mkdir(parents=True, exist_ok=True)


def _write_project_file(output_dir: Path, spec: dict[str, Any], result: DbtGenerationResult) -> None:
    yaml = require_yaml()
    pre_hooks, post_hooks = _job_hooks(spec)
    project = {
        "name": "type_materialisation_generated",
        "version": "1.0",
        "config-version": 2,
        "profile": "type_materialisation_generated",
        "model-paths": ["models"],
        "macro-paths": ["macros"],
        "on-run-start": pre_hooks,
        "on-run-end": post_hooks,
        "models": {
            "type_materialisation_generated": {
                "generated": {
                    "+materialized": "view",
                }
            }
        },
    }
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
            f"{{{{ config(materialized='view', alias='{model_name}') }}}}",
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
    failure_expression = _failure_details_expression(spec)
    select_lines = []
    for field in fields(spec):
        expression = _field_expression(field)
        data_type = field["data_type"]
        select_lines.append(f"    cast({expression} as {data_type}) as {_quote_identifier(field['id'])}")
    select_lines.extend(
        [
            "    cast('{{ var(\"audit_data_process_key\", \"manual\") }}' "
            f"as {GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}) as audit_data_process_key",
            f"    cast(current_timestamp() as {GENERATED_METADATA_FIELD_TYPES['audit_created_datetime']}) "
            "as audit_created_datetime",
            f"    cast(current_timestamp() as {GENERATED_METADATA_FIELD_TYPES['audit_last_changed_datetime']}) "
            "as audit_last_changed_datetime",
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
            "valid_rows as (",
            "    select *",
            "    from source_rows",
            f"    where {failure_expression} is null",
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
        f"    schema='{schema}',",
        f"    alias='{alias}',",
    ]
    if database:
        config_lines.insert(1, f"    database='{database}',")
    if extra_config_lines:
        config_lines.extend(extra_config_lines)
    return config_lines


def _write_quarantine_model(spec: dict[str, Any], result: DbtGenerationResult) -> None:
    target = spec["target"]
    quarantine = _quarantine_relation_config(spec)
    model_name = _quarantine_model_name(target["id"])
    source_model = _source_model_name(target["id"])
    failure_expression = _failure_details_expression(spec)
    source_columns = _source_output_columns(spec)
    select_lines = [
        "    cast(current_timestamp() as datetime) as loaded_at",
        "    cast('{{ var(\"job_id\", invocation_id) }}' as varchar(64)) as job_id",
        "    failure_details",
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
            "failed_rows as (",
            "    select",
            "        *,",
            f"        {failure_expression} as failure_details",
            "    from source_rows",
            ")",
            "",
            "select",
            ",\n".join(select_lines),
            "from failed_rows",
            "where failure_details is not null",
            "",
        ]
    )
    _write(result.output_dir / "models" / "generated" / f"{model_name}.sql", sql, result)


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
    unit_tests = [
        {
            "name": f"{target['id']}_sample_source",
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
    if _quarantine_enabled(spec):
        unit_tests.append(
            {
                "name": f"{_quarantine_model_name(target['id'])}_sample_source",
                "model": _quarantine_model_name(target["id"]),
                "given": [
                    {
                        "input": f"ref('{_source_model_name(target['id'])}')",
                        "rows": source_rows,
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


def _failure_details_expression(spec: dict[str, Any]) -> str:
    expressions = []
    for field in fields(spec):
        expressions.extend(_field_failure_expressions(field))
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


def _table_source_relation(source: dict[str, Any]) -> str:
    parts = []
    if source.get("database"):
        parts.append(str(source["database"]))
    parts.extend([str(source["schema"]), str(source["table"])])
    return ".".join(parts)


def _stage_reference(value: str) -> str:
    return value if value.startswith("@") else f"@{value}"


def _macro_object_name(macro_name: str) -> str:
    return macro_name.rpartition(".")[2]


def _quote_identifier(value: str) -> str:
    return value


def _quarantine_enabled(spec: dict[str, Any]) -> bool:
    control_data = spec.get("control_data", {})
    return isinstance(control_data, dict) and control_data.get("failure_mode") == "quarantine_row"


def _target_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = spec["target"]
    return RelationConfig(
        database=target.get("database"),
        schema=target["schema"],
        table=target.get("table_name", target["id"]),
    )


def _quarantine_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = _target_relation_config(spec)
    quarantine = spec.get("control_data", {}).get("quarantine", {})
    if not isinstance(quarantine, dict):
        quarantine = {}
    return RelationConfig(
        database=quarantine.get("database", target.database),
        schema=quarantine.get("schema", target.schema),
        table=quarantine.get("table", f"{target.table}_QUARANTINE"),
    )


def _job_relation_config(spec: dict[str, Any]) -> RelationConfig:
    target = _target_relation_config(spec)
    job = spec.get("control_data", {}).get("job", {})
    if not isinstance(job, dict):
        job = {}
    return RelationConfig(
        database=job.get("database", target.database),
        schema=job.get("schema", "BUSINESS"),
        table=job.get("table", "TYPE_MATERIALISATION_JOBS"),
    )


def _relation_name(relation: RelationConfig) -> str:
    parts = []
    if relation.database:
        parts.append(relation.database)
    parts.extend([relation.schema, relation.table])
    return ".".join(parts)


def _source_output_columns(spec: dict[str, Any]) -> list[str]:
    target_fields = fields(spec)
    if spec["source"]["format"] == "csv":
        target_fields = sorted(
            target_fields,
            key=lambda field: field.get("source", {}).get("pos", 999999),
        )
    return [field.get("source", {}).get("column", field["id"]) for field in target_fields]


def _job_hooks(spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    relation = _relation_name(_job_relation_config(spec))
    result_expression = (
        "{% set failed_result_count = "
        "(results | selectattr('status', 'equalto', 'error') | list | length) + "
        "(results | selectattr('status', 'equalto', 'fail') | list | length) %}"
        "{% if failed_result_count > 0 %}'FAILED'{% else %}'{{ var(\"job_result\", \"COMPLETED\") }}'{% endif %}"
    )
    create_sql = (
        f"create table if not exists {relation} ("
        "job_id varchar(64), "
        "event_type varchar(32), "
        "event_timestamp datetime, "
        "result varchar(64), "
        f"audit_data_process_key {GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}, "
        f"audit_created_datetime {GENERATED_METADATA_FIELD_TYPES['audit_created_datetime']}, "
        f"audit_last_changed_datetime {GENERATED_METADATA_FIELD_TYPES['audit_last_changed_datetime']}"
        ")"
    )
    start_sql = (
        f"insert into {relation} "
        "(job_id, event_type, event_timestamp, result, audit_data_process_key, "
        "audit_created_datetime, audit_last_changed_datetime) select "
        "cast('{{ var(\"job_id\", invocation_id) }}' as varchar(64)), "
        "'JOB_START', "
        "cast(current_timestamp() as datetime), "
        "null, "
        f"cast('{{{{ var(\"audit_data_process_key\", \"manual\") }}}}' as "
        f"{GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}), "
        "cast(current_timestamp() as datetime), "
        "cast(current_timestamp() as datetime)"
    )
    end_sql = (
        f"insert into {relation} "
        "(job_id, event_type, event_timestamp, result, audit_data_process_key, "
        "audit_created_datetime, audit_last_changed_datetime) select "
        "cast('{{ var(\"job_id\", invocation_id) }}' as varchar(64)), "
        "'JOB_END', "
        "cast(current_timestamp() as datetime), "
        f"{result_expression}, "
        f"cast('{{{{ var(\"audit_data_process_key\", \"manual\") }}}}' as "
        f"{GENERATED_METADATA_FIELD_TYPES['audit_data_process_key']}), "
        "cast(current_timestamp() as datetime), "
        "cast(current_timestamp() as datetime)"
    )
    return [create_sql, start_sql], [end_sql]


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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
